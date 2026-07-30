// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title ObsidianCipher
/// @notice A permissionless commit-reveal registry with a refundable stake.
///
/// A committer publishes only the hash of a secret, optionally locking ETH
/// behind it. Later they reveal the preimage; the contract verifies it and
/// credits the stake back for withdrawal. If the reveal window lapses without
/// a reveal, anyone may sweep the commitment and claim the stake as a bounty,
/// which makes abandoned commitments self-cleaning.
///
/// Two properties keep the flow front-running resistant:
///
///  - The commitment hash binds the committer's address and this deployment,
///    so a preimage seen in the mempool cannot be replayed by an observer.
///  - Commitments are stored per committer, so an observer who copies a
///    commitment hash out of a pending {seal} only ever occupies their own
///    slot and cannot block the rightful committer.
///
/// There is no owner, no upgrade path and no privileged role: every function
/// is callable by anyone, and the only funds the contract holds are stakes
/// that are already earmarked for a specific address.
contract ObsidianCipher {
    /// @notice Lifecycle of a single commitment.
    enum Status {
        None,
        Sealed,
        Revealed,
        Swept
    }

    /// @dev Packs into a single storage slot: 96 + 64 + 8 bits.
    struct Commitment {
        uint96 stake;
        uint64 revealDeadline;
        Status status;
    }

    /// @notice Shortest reveal window a committer may choose.
    uint64 public constant MIN_REVEAL_WINDOW = 10 minutes;
    /// @notice Longest reveal window a committer may choose.
    uint64 public constant MAX_REVEAL_WINDOW = 30 days;
    /// @notice Upper bound on a revealed preimage, to keep calldata sane.
    uint256 public constant MAX_SECRET_LENGTH = 1024;

    /// @dev Commitments keyed by {slotOf}(committer, commitment).
    mapping(bytes32 => Commitment) private _commitments;
    /// @notice ETH credited to an address and awaiting withdrawal.
    mapping(address => uint256) public balanceOf;
    /// @notice Number of commitments an address has ever sealed.
    mapping(address => uint256) public sealedCount;

    /// @notice Total ETH currently locked in unresolved commitments.
    uint256 public totalStaked;

    event Sealed(
        address indexed committer,
        bytes32 indexed commitment,
        uint256 stake,
        uint64 revealDeadline
    );
    event Revealed(
        address indexed committer,
        bytes32 indexed commitment,
        bytes secret,
        uint256 stake
    );
    event Swept(
        address indexed committer,
        bytes32 indexed commitment,
        address indexed keeper,
        uint256 bounty
    );
    event Withdrawn(address indexed account, uint256 amount);

    error CommitmentExists(bytes32 commitment);
    error CommitmentUnknown(bytes32 commitment);
    error CommitmentResolved(bytes32 commitment);
    error EmptyCommitment();
    error PreimageTooLong(uint256 length);
    error RevealWindowOutOfRange(uint64 window);
    error RevealWindowOpen(uint64 deadline);
    error RevealWindowClosed(uint64 deadline);
    error StakeTooLarge(uint256 stake);
    error NothingToWithdraw();
    error TransferFailed(address to, uint256 amount);

    /// @notice Seal a commitment, optionally backing it with an ETH stake.
    /// @dev The contract cannot check that `commitment` really is a hash of
    ///      the caller's secret — that is the whole point of a commitment —
    ///      so the entry is filed under the caller. Copying someone else's
    ///      pending commitment hash therefore only fills the copier's own
    ///      slot, and their copy is unrevealable and eventually sweepable.
    /// @param commitment Hash produced by {hashSecret} for `msg.sender`.
    /// @param revealWindow Seconds the committer has to reveal, from now.
    /// @return revealDeadline Timestamp after which the commitment is sweepable.
    function seal(bytes32 commitment, uint64 revealWindow)
        external
        payable
        returns (uint64 revealDeadline)
    {
        if (commitment == bytes32(0)) revert EmptyCommitment();
        if (revealWindow < MIN_REVEAL_WINDOW || revealWindow > MAX_REVEAL_WINDOW) {
            revert RevealWindowOutOfRange(revealWindow);
        }
        if (msg.value > type(uint96).max) revert StakeTooLarge(msg.value);

        bytes32 slot = slotOf(msg.sender, commitment);
        if (_commitments[slot].status != Status.None) {
            revert CommitmentExists(commitment);
        }

        revealDeadline = uint64(block.timestamp) + revealWindow;
        _commitments[slot] = Commitment({
            stake: uint96(msg.value),
            revealDeadline: revealDeadline,
            status: Status.Sealed
        });

        unchecked {
            // Bounded by the number of commitments, which cannot overflow.
            ++sealedCount[msg.sender];
        }
        totalStaked += msg.value;

        emit Sealed(msg.sender, commitment, msg.value, revealDeadline);
    }

    /// @notice Reveal a sealed secret and credit its stake back to the committer.
    /// @dev The stake is credited rather than sent, so a committer with a
    ///      reverting receive hook cannot brick their own reveal.
    /// @param secret The preimage that was committed to.
    /// @param salt The salt that was mixed into the commitment.
    /// @return commitment The commitment hash that was resolved.
    function reveal(bytes calldata secret, bytes32 salt)
        external
        returns (bytes32 commitment)
    {
        if (secret.length > MAX_SECRET_LENGTH) revert PreimageTooLong(secret.length);

        commitment = hashSecret(msg.sender, secret, salt);
        Commitment storage entry = _commitments[slotOf(msg.sender, commitment)];

        if (entry.status == Status.None) revert CommitmentUnknown(commitment);
        if (entry.status != Status.Sealed) revert CommitmentResolved(commitment);
        if (block.timestamp > entry.revealDeadline) {
            revert RevealWindowClosed(entry.revealDeadline);
        }

        uint256 stake = entry.stake;
        entry.status = Status.Revealed;
        entry.stake = 0;

        if (stake != 0) {
            totalStaked -= stake;
            balanceOf[msg.sender] += stake;
        }

        emit Revealed(msg.sender, commitment, secret, stake);
    }

    /// @notice Sweep a commitment whose reveal window has lapsed.
    /// @dev Callable by anyone; the forfeited stake is credited to the caller
    ///      as a keeper bounty. Commitments with no stake can still be swept,
    ///      which settles their status on-chain.
    /// @param committer The address that sealed the commitment.
    /// @param commitment The commitment hash to sweep.
    /// @return bounty The stake credited to the caller.
    function sweep(address committer, bytes32 commitment)
        external
        returns (uint256 bounty)
    {
        Commitment storage entry = _commitments[slotOf(committer, commitment)];

        if (entry.status == Status.None) revert CommitmentUnknown(commitment);
        if (entry.status != Status.Sealed) revert CommitmentResolved(commitment);
        if (block.timestamp <= entry.revealDeadline) {
            revert RevealWindowOpen(entry.revealDeadline);
        }

        bounty = entry.stake;
        entry.status = Status.Swept;
        entry.stake = 0;

        if (bounty != 0) {
            totalStaked -= bounty;
            balanceOf[msg.sender] += bounty;
        }

        emit Swept(committer, commitment, msg.sender, bounty);
    }

    /// @notice Withdraw the caller's credited balance.
    /// @dev Pull payments: the balance is zeroed before the call, so a
    ///      reentrant withdrawal finds nothing left to take.
    /// @return amount The ETH sent to the caller.
    function withdraw() external returns (uint256 amount) {
        amount = balanceOf[msg.sender];
        if (amount == 0) revert NothingToWithdraw();

        balanceOf[msg.sender] = 0;

        (bool ok, ) = msg.sender.call{value: amount}("");
        if (!ok) revert TransferFailed(msg.sender, amount);

        emit Withdrawn(msg.sender, amount);
    }

    /// @notice Compute the commitment hash for a secret.
    /// @dev Off-chain callers should use this to build the value passed to
    ///      {seal}. The committer, chain and contract address are bound in, so
    ///      a revealed preimage cannot be replayed by another account, on
    ///      another chain, or against another deployment.
    function hashSecret(address committer, bytes memory secret, bytes32 salt)
        public
        view
        returns (bytes32)
    {
        return keccak256(abi.encode(block.chainid, address(this), committer, secret, salt));
    }

    /// @notice Storage key a commitment is filed under.
    function slotOf(address committer, bytes32 commitment)
        public
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(committer, commitment));
    }

    /// @notice Read a commitment.
    function commitmentOf(address committer, bytes32 commitment)
        external
        view
        returns (Commitment memory)
    {
        Commitment memory entry = _commitments[slotOf(committer, commitment)];
        if (entry.status == Status.None) revert CommitmentUnknown(commitment);
        return entry;
    }

    /// @notice Whether a commitment is currently sweepable.
    function isSweepable(address committer, bytes32 commitment)
        external
        view
        returns (bool)
    {
        Commitment storage entry = _commitments[slotOf(committer, commitment)];
        return entry.status == Status.Sealed && block.timestamp > entry.revealDeadline;
    }

    /// @notice ETH held by the contract that is not backing a live commitment.
    /// @dev Equals the sum of all withdrawable balances. Forced ETH (a
    ///      selfdestruct beneficiary or a coinbase payout) inflates this
    ///      without being claimable, which is harmless: nothing in the
    ///      contract reads `address(this).balance` to make a decision.
    function unlockedBalance() external view returns (uint256) {
        return address(this).balance - totalStaked;
    }
}
