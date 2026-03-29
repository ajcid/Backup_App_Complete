import os
import json
import subprocess
import time
import logging
import signal
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from datetime import datetime
import threading
import fcntl  # Para bloqueio de ficheiros em Linux

# Configuração de Logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("data/logs/sistema_principal.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'chave_secreta_para_sessoes_inspecao'

# Caminhos de arquivos
CONFIG_FILE = 'data/config_inspecao.json'
USERS_FILE = 'data/users.json'
MOSAIC_SETTINGS = 'data/mosaic_settings.json'

# Lock global para operações de ficheiro
file_lock = threading.Lock()

def safe_load_json(filepath, default_value):
    """Lê um arquivo JSON com segurança contra corrupção e acessos simultâneos."""
    with file_lock:
        if not os.path.exists(filepath):
            return default_value
        
        try:
            with open(filepath, 'r') as f:
                # Tenta obter lock exclusivo do SO
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
                return data
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Erro ao carregar {filepath}: {e}")
            # Se o arquivo estiver corrompido (tamanho 0), tenta recuperar do backup se existir
            return default_value

def safe_save_json(filepath, data):
    """Guarda um arquivo JSON de forma atómica para evitar corrupção."""
    with file_lock:
        temp_path = f"{filepath}.tmp"
        try:
            with open(temp_path, 'w') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
                fcntl.flock(f, fcntl.LOCK_UN)
            
            # Operação atómica de renomear
            os.replace(temp_path, filepath)
            logger.info(f"Configuração salva com sucesso em {filepath}")
            return True
        except Exception as e:
            logger.error(f"Erro ao salvar {filepath}: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

def load_config():
    default = {
        "backup_enabled": False,
        "mosaic_enabled": False,
        "lines": {}
    }
    return safe_load_json(CONFIG_FILE, default)

def save_config(config):
    return safe_save_json(CONFIG_FILE, config)

def load_users():
    return safe_load_json(USERS_FILE, {})

def get_service_status(pid_file):
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OverflowError, OSError):
        return False

@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    config = load_config()
    return render_template('index.html', config=config)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        
        if username in users and users[username] == password:
            session['user'] = username
            logger.info(f"Usuário {username} fez login.")
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Credenciais inválidas")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/service_status')
def service_status():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    config = load_config()
    status = {
        "backup": get_service_status('backup_server.pid'),
        "mirror": get_service_status('mirror_ssd.pid'),
        "mosaic": config.get("mosaic_enabled", False),
        "config": config
    }
    return jsonify(status)

@app.route('/toggle_backup', methods=['POST'])
def toggle_backup():
    if 'user' not in session:
        return jsonify({"success": False}), 401
    
    config = load_config()
    current_state = config.get("backup_enabled", False)
    new_state = not current_state
    config["backup_enabled"] = new_state
    
    if save_config(config):
        if new_state:
            subprocess.Popen(["python3", "backup_app_complete.py"])
            logger.info("Serviço de Backup iniciado via UI")
        else:
            if os.path.exists('backup_server.pid'):
                try:
                    with open('backup_server.pid', 'r') as f:
                        pid = int(f.read().strip())
                    os.kill(pid, signal.SIGTERM)
                    logger.info("Serviço de Backup parado via UI")
                except:
                    pass
        return jsonify({"success": True, "state": new_state})
    return jsonify({"success": False, "error": "Erro ao salvar config"})

@app.route('/toggle_mosaic', methods=['POST'])
def toggle_mosaic():
    if 'user' not in session:
        return jsonify({"success": False}), 401
    
    config = load_config()
    new_state = not config.get("mosaic_enabled", False)
    config["mosaic_enabled"] = new_state
    
    if save_config(config):
        return jsonify({"success": True, "state": new_state})
    return jsonify({"success": False})

@app.route('/save_lines_config', methods=['POST'])
def save_lines_config():
    if 'user' not in session:
        return jsonify({"success": False}), 401
    
    try:
        lines_data = request.json.get('lines', {})
        config = load_config()
        config['lines'] = lines_data
        if save_config(config):
            return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Erro ao salvar linhas: {e}")
    return jsonify({"success": False})

@app.route('/check_path_access', methods=['POST'])
def check_path_access():
    path = request.json.get('path', '')
    if not path:
        return jsonify({"access": False})
    
    exists = os.path.exists(path)
    readable = os.access(path, os.R_OK) if exists else False
    return jsonify({"access": exists and readable, "exists": exists})

@app.route('/logs')
def get_logs():
    if 'user' not in session:
        return "Unauthorized", 401
    
    log_path = "data/logs/sistema_principal.log"
    if not os.path.exists(log_path):
        return "Arquivo de log não encontrado."
    
    try:
        with open(log_path, 'r') as f:
            # Retorna as últimas 200 linhas
            lines = f.readlines()
            return "".join(lines[-200:])
    except Exception as e:
        return f"Erro ao ler logs: {str(e)}"

@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    if 'user' not in session:
        return jsonify({"success": False}), 401
    
    log_path = "data/logs/sistema_principal.log"
    try:
        with open(log_path, 'w') as f:
            f.write(f"{datetime.now()} - INFO - Log limpo pelo usuário {session['user']}\n")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    # Garantir que as pastas de dados e logs existem
    os.makedirs('data/logs', exist_ok=True)
    
    # Se o arquivo de config não existir ou estiver vazio, cria um default
    if not os.path.exists(CONFIG_FILE) or os.path.getsize(CONFIG_FILE) == 0:
        save_config({
            "backup_enabled": False, 
            "mosaic_enabled": False, 
            "lines": {}
        })

    app.run(host='0.0.0.0', port=5000, debug=False)