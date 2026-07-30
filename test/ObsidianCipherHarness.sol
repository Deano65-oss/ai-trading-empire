// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ObsidianCipher} from "../contracts/ObsidianCipher.sol";

/// @dev A second account that sweeps expired commitments for the bounty.
contract Keeper {
    ObsidianCipher public immutable cipher;

    constructor(ObsidianCipher cipher_) {
        cipher = cipher_;
    }

    function sweep(address committer, bytes32 commitment) external returns (uint256) {
        return cipher.sweep(committer, commitment);
    }

    function withdraw() external returns (uint256) {
        return cipher.withdraw();
    }

    receive() external payable {}
}

/// @dev An observer that copies a commitment hash out of the mempool and tries
///      to seal it first, or to reveal a secret it saw someone else publish.
contract Griefer {
    ObsidianCipher public immutable cipher;

    constructor(ObsidianCipher cipher_) {
        cipher = cipher_;
    }

    function copySeal(bytes32 commitment, uint64 window) external payable {
        cipher.seal{value: msg.value}(commitment, window);
    }

    function copyReveal(bytes calldata secret, bytes32 salt) external returns (bytes32) {
        return cipher.reveal(secret, salt);
    }
}

/// @dev A committer whose receive hook is hostile: it either reverts outright
///      or attempts to re-enter {ObsidianCipher-withdraw}.
contract HostileReceiver {
    ObsidianCipher public immutable cipher;
    bool public reenter;
    uint256 public reentryAttempts;
    /// @dev Selector the re-entrant call reverted with, or 0 if it succeeded.
    bytes4 public reentryError;

    constructor(ObsidianCipher cipher_) {
        cipher = cipher_;
    }

    function seal(bytes32 commitment, uint64 window) external payable {
        cipher.seal{value: msg.value}(commitment, window);
    }

    function reveal(bytes calldata secret, bytes32 salt) external {
        cipher.reveal(secret, salt);
    }

    function withdraw() external returns (uint256) {
        return cipher.withdraw();
    }

    function setReenter(bool value) external {
        reenter = value;
    }

    receive() external payable {
        if (!reenter) revert("hostile");

        // Swallow the failure so the outer withdrawal still completes: that
        // way the test can prove the re-entrant call got nothing *and* that
        // the honest payment was made exactly once.
        ++reentryAttempts;
        try cipher.withdraw() returns (uint256) {
            reentryError = bytes4(0);
        } catch (bytes memory reason) {
            reentryError = bytes4(reason);
        }
    }
}

/// @notice Executable test suite for {ObsidianCipher}.
///
/// Every check is a `require`, so a failing assertion reverts the whole
/// transaction and the runner sees a failed tx. `checksPassed` is the number
/// of assertions that held, and each phase emits a {PhaseComplete}.
///
/// The suite is split into two phases because some behaviour only exists after
/// a reveal window lapses; the runner mines the second phase at a later block
/// timestamp.
contract ObsidianCipherHarness {
    ObsidianCipher public cipher;
    Keeper public keeper;
    Griefer public griefer;
    HostileReceiver public hostile;

    uint256 public checksPassed;
    uint256 public phase;

    bytes32 public expiringCommitment;
    bytes32 public copiedCommitment;
    bytes32 public hostileCommitment;

    bytes constant SECRET_A = "obsidian-cipher/secret-a";
    bytes constant SECRET_B = "obsidian-cipher/secret-b";
    bytes constant SECRET_C = "obsidian-cipher/secret-c";
    bytes constant SECRET_D = "obsidian-cipher/secret-d";
    bytes32 constant SALT_A = keccak256("salt-a");
    bytes32 constant SALT_B = keccak256("salt-b");
    bytes32 constant SALT_C = keccak256("salt-c");
    bytes32 constant SALT_D = keccak256("salt-d");

    uint64 constant SHORT_WINDOW = 10 minutes;
    uint256 constant STAKE = 1 ether;
    uint256 constant EXPIRING_STAKE = 0.5 ether;

    event Check(uint256 indexed index, string what);
    event PhaseComplete(uint256 indexed phase, uint256 checksPassed);

    error PhaseOutOfOrder(uint256 expected, uint256 actual);

    constructor() payable {
        cipher = new ObsidianCipher();
        keeper = new Keeper(cipher);
        griefer = new Griefer(cipher);
        hostile = new HostileReceiver(cipher);
    }

    receive() external payable {}

    function _check(bool ok, string memory what) private {
        require(ok, what);
        unchecked {
            ++checksPassed;
        }
        emit Check(checksPassed, what);
    }

    /// @dev Asserts that a call reverted with a specific custom error selector.
    function _expectError(bytes memory reason, bytes4 selector, string memory what) private {
        _check(reason.length >= 4, string.concat(what, ": empty revert"));
        bytes4 got = bytes4(reason);
        _check(got == selector, string.concat(what, ": wrong error"));
    }

    // ------------------------------------------------------------------
    // Phase 1: seal, input validation, reveal, withdraw, front-running.
    // ------------------------------------------------------------------
    function phase1() external {
        if (phase != 0) revert PhaseOutOfOrder(0, phase);
        phase = 1;

        // --- seal -------------------------------------------------------
        bytes32 cA = cipher.hashSecret(address(this), SECRET_A, SALT_A);
        uint64 deadline = cipher.seal{value: STAKE}(cA, 1 hours);

        _check(deadline == uint64(block.timestamp) + 1 hours, "seal: deadline");
        _check(cipher.totalStaked() == STAKE, "seal: totalStaked");
        _check(cipher.sealedCount(address(this)) == 1, "seal: sealedCount");
        _check(address(cipher).balance == STAKE, "seal: contract balance");

        ObsidianCipher.Commitment memory entry = cipher.commitmentOf(address(this), cA);
        _check(entry.stake == STAKE, "seal: stored stake");
        _check(entry.revealDeadline == deadline, "seal: stored deadline");
        _check(entry.status == ObsidianCipher.Status.Sealed, "seal: stored status");
        _check(!cipher.isSweepable(address(this), cA), "seal: not yet sweepable");

        // --- seal input validation --------------------------------------
        try cipher.seal(cA, 1 hours) returns (uint64) {
            revert("seal: duplicate accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentExists.selector, "seal: duplicate");
        }

        try cipher.seal(bytes32(0), 1 hours) returns (uint64) {
            revert("seal: empty accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.EmptyCommitment.selector, "seal: empty");
        }

        try cipher.seal(keccak256("too-short"), SHORT_WINDOW - 1) returns (uint64) {
            revert("seal: short window accepted");
        } catch (bytes memory reason) {
            _expectError(
                reason, ObsidianCipher.RevealWindowOutOfRange.selector, "seal: short window"
            );
        }

        try cipher.seal(keccak256("too-long"), 30 days + 1) returns (uint64) {
            revert("seal: long window accepted");
        } catch (bytes memory reason) {
            _expectError(
                reason, ObsidianCipher.RevealWindowOutOfRange.selector, "seal: long window"
            );
        }

        // --- reveal validation -------------------------------------------
        try cipher.reveal(SECRET_A, SALT_B) returns (bytes32) {
            revert("reveal: wrong salt accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentUnknown.selector, "reveal: wrong salt");
        }

        try cipher.reveal(SECRET_B, SALT_A) returns (bytes32) {
            revert("reveal: wrong secret accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentUnknown.selector, "reveal: wrong secret");
        }

        // A third party who watched the preimage go by cannot claim it: the
        // hash binds the committer, so the griefer's own slot is empty.
        try griefer.copyReveal(SECRET_A, SALT_A) returns (bytes32) {
            revert("reveal: stolen preimage accepted");
        } catch (bytes memory reason) {
            _expectError(
                reason, ObsidianCipher.CommitmentUnknown.selector, "reveal: stolen preimage"
            );
        }

        // Sweeping before the deadline is refused.
        try cipher.sweep(address(this), cA) returns (uint256) {
            revert("sweep: early accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.RevealWindowOpen.selector, "sweep: early");
        }

        // --- reveal --------------------------------------------------------
        bytes32 revealed = cipher.reveal(SECRET_A, SALT_A);
        _check(revealed == cA, "reveal: returned commitment");
        _check(cipher.totalStaked() == 0, "reveal: totalStaked cleared");
        _check(cipher.balanceOf(address(this)) == STAKE, "reveal: stake credited");
        _check(cipher.unlockedBalance() == STAKE, "reveal: unlocked balance");

        entry = cipher.commitmentOf(address(this), cA);
        _check(entry.status == ObsidianCipher.Status.Revealed, "reveal: status");
        _check(entry.stake == 0, "reveal: stake zeroed");

        try cipher.reveal(SECRET_A, SALT_A) returns (bytes32) {
            revert("reveal: replay accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentResolved.selector, "reveal: replay");
        }

        try cipher.sweep(address(this), cA) returns (uint256) {
            revert("sweep: revealed accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentResolved.selector, "sweep: revealed");
        }

        // --- withdraw --------------------------------------------------------
        uint256 before = address(this).balance;
        uint256 amount = cipher.withdraw();
        _check(amount == STAKE, "withdraw: amount");
        _check(address(this).balance == before + STAKE, "withdraw: eth received");
        _check(cipher.balanceOf(address(this)) == 0, "withdraw: balance cleared");
        _check(address(cipher).balance == 0, "withdraw: contract drained");

        try cipher.withdraw() returns (uint256) {
            revert("withdraw: empty accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.NothingToWithdraw.selector, "withdraw: empty");
        }

        // --- front-running a seal ---------------------------------------------
        // The griefer copies a commitment hash out of a pending seal. Because
        // entries are filed per committer, the rightful committer is unaffected.
        copiedCommitment = cipher.hashSecret(address(this), SECRET_B, SALT_B);
        griefer.copySeal(copiedCommitment, SHORT_WINDOW);

        uint64 ownDeadline = cipher.seal{value: 0}(copiedCommitment, 1 hours);
        _check(ownDeadline > uint64(block.timestamp), "frontrun: own seal survives");

        bytes32 secondReveal = cipher.reveal(SECRET_B, SALT_B);
        _check(secondReveal == copiedCommitment, "frontrun: own reveal survives");
        _check(
            cipher.commitmentOf(address(griefer), copiedCommitment).status
                == ObsidianCipher.Status.Sealed,
            "frontrun: griefer copy still stuck"
        );

        // --- unknown commitments ------------------------------------------------
        try cipher.sweep(address(this), keccak256("never-sealed")) returns (uint256) {
            revert("sweep: unknown accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentUnknown.selector, "sweep: unknown");
        }

        try cipher.commitmentOf(address(this), keccak256("never-sealed")) returns (
            ObsidianCipher.Commitment memory
        ) {
            revert("commitmentOf: unknown accepted");
        } catch (bytes memory reason) {
            _expectError(
                reason, ObsidianCipher.CommitmentUnknown.selector, "commitmentOf: unknown"
            );
        }

        // --- set up state that phase 2 needs ---------------------------------------
        expiringCommitment = cipher.hashSecret(address(this), SECRET_C, SALT_C);
        cipher.seal{value: EXPIRING_STAKE}(expiringCommitment, SHORT_WINDOW);
        _check(cipher.totalStaked() == EXPIRING_STAKE, "expiring: staked");

        hostileCommitment = cipher.hashSecret(address(hostile), SECRET_D, SALT_D);
        hostile.seal{value: STAKE}(hostileCommitment, 1 hours);
        hostile.reveal(SECRET_D, SALT_D);
        _check(cipher.balanceOf(address(hostile)) == STAKE, "hostile: credited");

        emit PhaseComplete(1, checksPassed);
    }

    // ------------------------------------------------------------------
    // Phase 2: expiry, sweeping, and hostile withdrawal paths.
    // Must be mined after the short reveal window has lapsed.
    // ------------------------------------------------------------------
    function phase2() external {
        if (phase != 1) revert PhaseOutOfOrder(1, phase);
        phase = 2;

        _check(cipher.isSweepable(address(this), expiringCommitment), "expiry: sweepable");

        // The window has closed, so the committer can no longer reveal.
        try cipher.reveal(SECRET_C, SALT_C) returns (bytes32) {
            revert("expiry: late reveal accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.RevealWindowClosed.selector, "expiry: late reveal");
        }

        // Anyone may sweep, and the bounty goes to the sweeper.
        uint256 bounty = keeper.sweep(address(this), expiringCommitment);
        _check(bounty == EXPIRING_STAKE, "sweep: bounty");
        _check(cipher.balanceOf(address(keeper)) == EXPIRING_STAKE, "sweep: keeper credited");
        _check(cipher.balanceOf(address(this)) == 0, "sweep: committer not credited");
        _check(cipher.totalStaked() == 0, "sweep: totalStaked cleared");
        _check(
            cipher.commitmentOf(address(this), expiringCommitment).status
                == ObsidianCipher.Status.Swept,
            "sweep: status"
        );

        try keeper.sweep(address(this), expiringCommitment) returns (uint256) {
            revert("sweep: double sweep accepted");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.CommitmentResolved.selector, "sweep: double");
        }

        uint256 keeperBefore = address(keeper).balance;
        _check(keeper.withdraw() == EXPIRING_STAKE, "sweep: keeper withdraw");
        _check(address(keeper).balance == keeperBefore + EXPIRING_STAKE, "sweep: keeper paid");

        // A zero-stake copy is still sweepable, which settles it on-chain.
        uint256 zeroBounty = keeper.sweep(address(griefer), copiedCommitment);
        _check(zeroBounty == 0, "sweep: zero bounty");
        _check(
            cipher.commitmentOf(address(griefer), copiedCommitment).status
                == ObsidianCipher.Status.Swept,
            "sweep: zero-stake status"
        );

        // --- hostile withdrawal paths -------------------------------------------
        // A committer that rejects ETH cannot be paid, but the failure is
        // contained: the revert leaves its credit intact.
        try hostile.withdraw() returns (uint256) {
            revert("hostile: withdraw succeeded");
        } catch (bytes memory reason) {
            _expectError(reason, ObsidianCipher.TransferFailed.selector, "hostile: transfer failed");
        }
        _check(cipher.balanceOf(address(hostile)) == STAKE, "hostile: credit intact");

        // Now the hook accepts the ETH but re-enters withdraw() from inside the
        // transfer. The balance was zeroed before the call, so the re-entrant
        // attempt finds nothing, and the honest payment lands exactly once.
        hostile.setReenter(true);
        uint256 hostileBefore = address(hostile).balance;
        _check(hostile.withdraw() == STAKE, "reentrancy: paid once");
        _check(address(hostile).balance == hostileBefore + STAKE, "reentrancy: exact amount");
        _check(hostile.reentryAttempts() == 1, "reentrancy: hook did re-enter");
        _check(
            hostile.reentryError() == ObsidianCipher.NothingToWithdraw.selector,
            "reentrancy: re-entrant call got nothing"
        );
        _check(cipher.balanceOf(address(hostile)) == 0, "reentrancy: credit cleared");

        _check(address(cipher).balance == 0, "final: contract drained");
        _check(cipher.unlockedBalance() == 0, "final: unlocked balance");
        _check(cipher.totalStaked() == 0, "final: nothing staked");

        emit PhaseComplete(2, checksPassed);
    }
}
