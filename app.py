from flask import Flask, request, jsonify
from openai import OpenAI
import os
import logging
import time
import shutil
import sqlite3
import psycopg2

app = Flask(__name__)
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY', ''))
logging.basicConfig(level=logging.INFO)

def auto_configure_env():
    required = ['KRAKEN_API_KEY', 'KRAKEN_API_SECRET', 'OPENAI_API_KEY', 'EMAIL_FROM', 'EMAIL_TO', 'EMAIL_USER', 'EMAIL_PASS', 'PG_DB', 'PG_USER', 'PG_PASS', 'PG_HOST', 'PG_PORT']
    missing = [var for var in required if not os.getenv(var)]
    return missing

def write_heartbeat():
    with open('heartbeat.txt', 'a') as f:
        f.write(f"Alive at {time.strftime('%Y-%m-%d %H:%M:%S BST')}\n")

def backup_database():
    if os.path.exists('trader.db'):
        shutil.copy('trader.db', f'trader_backup_{time.strftime("%Y%m%d_%H%M%S")}.db')

def build_system(command):
    prompt = f"Act as an AI builder for a crypto trading system targeting 2% daily growth from £10k. Command: {command}. Generate or update Python files (app.py, trading.py, agents.py, data.py, utils.py, requirements.txt) with full code. Include error handling, API connections (Kraken), Open AI for strategy, and database (SQLite, PostgreSQL). Ensure it’s deployable on Render with a /dashboard and /jarvis endpoint. Return only the complete file contents as a JSON object."
    response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    try:
        import json
        files = json.loads(response.choices[0].message.content)
        for filename, content in files.items():
            with open(filename, 'w') as f:
                f.write(content)
        return {"status": "Files updated", "files": list(files.keys())}
    except Exception as e:
        logging.error(f"Build failed: {e}")
        return {"status": "Error", "message": str(e)}

def check_system_health():
    try:
        conn_sqlite = sqlite3.connect('trader.db', check_same_thread=False)
        conn_postgres = psycopg2.connect(dbname=os.getenv('PG_DB'), user=os.getenv('PG_USER'), password=os.getenv('PG_PASS'), host=os.getenv('PG_HOST'), port=int(os.getenv('PG_PORT')))
        return "Healthy"
    except Exception as e:
        return f"Error: {e}"

@app.route('/builder', methods=['POST'])
def builder():
    command = request.json.get('command', '')
    if not command:
        return jsonify({"status": "Error", "message": "No command provided"})
    result = build_system(command)
    return jsonify(result)

@app.route('/health')
def health_check():
    write_heartbeat()
    backup_database()
    return "Kraken Trader Running", 200

@app.route('/dashboard')
def dashboard():
    return "Dashboard (under construction)", 200

if __name__ == "__main__":
    missing = auto_configure_env()
    if missing:
        logging.error(f"Missing env vars: {missing}")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))