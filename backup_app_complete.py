#!/usr/bin/env python3
"""
Sistema Completo de Gestão de Imagens e Backup
Versão Final com Sistema de Turnos, LEDs de Status, Portal Público Separado, 
Gestão de Users e Sistema Dinâmico de Modos (Teste vs Produção) On-The-Fly.

ATUALIZAÇÃO: Otimização extrema para NAS (Synology DS423).
Uso de os.scandir nativo, pré-compilação Regex e limitação de I/O Thrashing.
Adição do serviço de Backup PKIRIS, Históricos e Artigos com árvore automática e retentividade.
Correção das rotas API do Mosaico e tags Cross-Origin.
Adicionado arranque automático do portal de Criação Pen PKIRIS.
"""

import os
import json
import shutil
import threading
import logging
import time
import zipfile
import tempfile
import subprocess
import xml.etree.ElementTree as ET
import concurrent.futures
from datetime import datetime, timedelta
import psutil
import atexit
import socket
import sys
import platform
import uuid
import re
import base64
from flask import Flask, render_template_string, request, jsonify, send_file, redirect, url_for, session, send_from_directory, abort, has_request_context
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# Expressões regulares pré-compiladas para máxima velocidade na extração XML
REGEX_NOM_ART = re.compile(r'<NOM_ART[^>]*>(.*?)</NOM_ART>', re.IGNORECASE)

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS PRINCIPAIS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

BASE_CONFIG_PATH = DATA_DIR
BASE_LOG_PATH = os.path.join(DATA_DIR, "logs")
USER_DATA_PATH = os.path.join(BASE_CONFIG_PATH, "users.json")
ACTION_LOG_FOLDER = os.path.join(BASE_LOG_PATH, "action_logs")

os.makedirs(BASE_CONFIG_PATH, exist_ok=True)
os.makedirs(BASE_LOG_PATH, exist_ok=True)
os.makedirs(ACTION_LOG_FOLDER, exist_ok=True)

# ==============================================================================
# INICIALIZAÇÃO DOS SERVIDORES FLASK E LOGS
# ==============================================================================
app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = os.path.join(BASE_CONFIG_PATH, "backup_settings.json")
LOG_FILE = os.path.join(BASE_LOG_PATH, "backup_server.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ==============================================================================
# VARIÁVEIS GLOBAIS DE ESTADO E MEMÓRIA
# ==============================================================================
copy_threads = {}
stop_copy_flags = {}
mirror_thread = None
stop_mirror_flag = False
mosaic_processes = {}
mosaic_lock = threading.RLock()
public_portal_process = None
pen_pkiris_process = None
active_folders = {}
last_day_reset = datetime.now().day
files_copied_shift = {}
files_copied_day = {}
counters_lock = threading.Lock()
export_tasks = {}
file_io_lock = threading.Lock()

# Variáveis dos serviços adicionais (PKIRIS, Históricos, Artigos)
pkiris_thread = None
stop_pkiris_flag = False
historicos_thread = None
stop_historicos_flag = False
artigos_thread = None
stop_artigos_flag = False

analysis_status = {
    'running': False, 
    'progress': 0, 
    'eta': '--:--', 
    'status': 'idle', 
    'stop_flag': False,
    'total_files': 0,
    'files_done': 0,
    'current_file': '',
    'recent_logs': []
}
analysis_thread = None

article_db_lock = threading.Lock()
last_seen_article_state = {}

# ==============================================================================
# SISTEMA DE INTERNACIONALIZAÇÃO (i18n)
# ==============================================================================
SUPPORTED_LANGUAGES = ['pt', 'es', 'en', 'pl', 'bg']
DEFAULT_LANG = 'pt'

def safe_load_json(filepath, default_value):
    try:
        with file_io_lock:
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
    except Exception as e:
        logging.error(f"Erro ao carregar {filepath}: {e}")
    return default_value

def safe_save_json(filepath, data):
    try:
        with file_io_lock:
            temp_path = filepath + '.tmp'
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, filepath)
        return True
    except Exception as e:
        logging.error(f"Erro ao salvar {filepath}: {e}")
        if os.path.exists(filepath + '.tmp'):
            try: os.remove(filepath + '.tmp')
            except: pass
        return False

def init_translations():
    base_dict = {
        "pt": {"Gestão de Backups": "Gestão de Backups"},
        "en": {"Gestão de Backups": "Backup Management", "Utilizador": "Username", "Password": "Password", "Entrar no Painel": "Login to Dashboard"},
        "es": {"Gestão de Backups": "Gestión de Backups", "Utilizador": "Usuario", "Password": "Password", "Entrar no Painel": "Iniciar Sesión"},
        "pl": {"Gestão de Backups": "Zarządzanie Kopiami", "Utilizador": "Użytkownik", "Password": "Hasło", "Entrar no Painel": "Zaloguj"},
        "bg": {"Gestão de Backups": "Управление на архивите", "Utilizador": "Потребител", "Password": "Парола", "Entrar no Painel": "Вход"}
    }
    for lang in SUPPORTED_LANGUAGES:
        path = os.path.join(DATA_DIR, f'lang_{lang}.json')
        if not os.path.exists(path):
            safe_save_json(path, base_dict.get(lang, {}))

init_translations()

def load_translations(lang):
    path = os.path.join(DATA_DIR, f'lang_{lang}.json')
    return safe_load_json(path, {})

@app.context_processor
def inject_translator():
    lang = request.cookies.get('ui_lang', DEFAULT_LANG)
    translations = load_translations(lang)
    def t(text):
        return translations.get(text, text)
    return dict(t=t, lang=lang)

def _t(text):
    if has_request_context():
        lang = request.cookies.get('ui_lang', DEFAULT_LANG)
        return load_translations(lang).get(text, text)
    return text

@app.route('/set_lang', methods=['POST'])
def set_lang():
    data = request.get_json()
    if data and data.get('lang') in SUPPORTED_LANGUAGES:
        resp = jsonify({"success": True})
        resp.set_cookie('ui_lang', data['lang'], max_age=31536000)
        return resp
    return jsonify({"success": False}), 400

# ==============================================================================
# FUNÇÕES CORE DE SEGURANÇA E LEITURA
# ==============================================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def load_users():
    if not os.path.exists(USER_DATA_PATH) or os.path.getsize(USER_DATA_PATH) == 0:
        default_user = {"cid": {"password": generate_password_hash("109352"), "is_dev": True}}
        save_users(default_user)
        return default_user
    return safe_load_json(USER_DATA_PATH, {})

def save_users(users_data):
    return safe_save_json(USER_DATA_PATH, users_data)

def load_config():
    default_cfg = {
        "ssd_path": "/volume2/ssd_mirror",
        "mirror_source_path": "/volume1/inspecao_organizadas",
        "mosaic_config_folder": BASE_CONFIG_PATH,
        "log_file_path": LOG_FILE,
        "article_analysis_path": "/volume1/inspecao_organizadas",
        "mirror_include_subfolders": True,
        "ssd_retention_days": 5,
        "hdd_retention_months": 6,
        "scan_interval_sec": 1,
        "pkiris_retention_days": 5,
        "pkiris_dst_root": "",
        "historicos_retention_days": 365,
        "historicos_dst_root": "",
        "artigos_retention_days": 365,
        "artigos_dst_root": "",
        "turnos": {
            "turno1": {"inicio": "06:00", "fim": "14:00"},
            "turno2": {"inicio": "14:00", "fim": "22:00"},
            "turno3": {"inicio": "22:00", "fim": "06:00"}
        },
        "backup_enabled": True,
        "visao_global": {
            "port_lateral": 5098,
            "port_fundo": 5099,
            "cycle_mode_active": True,
            "cycle_time_sec": 30,
            "mosaic_lateral_active": False,
            "mosaic_fundo_active": False
        },
        "linhas": {
            "21": { 
                "cycle_mode_active": False, "cycle_time_sec": 30, "use_test_mode": False,
                "lateral": { "src": "", "dst": "", "src_prod": "", "dst_prod": "", "src_test": "", "dst_test": "", "pkiris_src": "", "historico_src": "", "artigo_src": "", "backup_active": True, "delete_source": True, "mosaic_active": False, "mosaic_port": 5001 },
                "fundo": { "src": "", "dst": "", "src_prod": "", "dst_prod": "", "src_test": "", "dst_test": "", "pkiris_src": "", "historico_src": "", "artigo_src": "", "backup_active": True, "delete_source": True, "mosaic_active": False, "mosaic_port": 5002 }
            }
        },
        "mosaic_source_path": "/volume1/inspecao_organizadas"
    }
    
    cfg = safe_load_json(CONFIG_FILE, default_cfg)
    
    if 'linhas' in cfg and '34' in cfg['linhas']:
        cfg['linhas']['34'].pop('1', None)
        cfg['linhas']['34'].pop('2', None)
        
    for key in default_cfg:
        if key not in cfg:
            cfg[key] = default_cfg[key]
            
    return cfg

def get_current_shift():
    config = load_config()
    turnos = config.get('turnos', {})
    now = datetime.now()
    current_time = now.strftime('%H:%M')
    for turno_name, turno_config in turnos.items():
        inicio = turno_config.get('inicio', '06:00')
        fim = turno_config.get('fim', '14:00')
        if inicio > fim:
            if current_time >= inicio or current_time < fim: return turno_name
        else:
            if inicio <= current_time < fim: return turno_name
    return 'turno1'

def get_current_shift_for_log():
    now = datetime.now().time()
    if now >= datetime.strptime("06:00", "%H:%M").time() and now < datetime.strptime("14:00", "%H:%M").time():
        return "turno1"
    elif now >= datetime.strptime("14:00", "%H:%M").time() and now < datetime.strptime("22:00", "%H:%M").time():
        return "turno2"
    else:
        return "turno3"

def log_user_action(username, action):
    try:
        current_shift = get_current_shift_for_log()
        today = datetime.now().strftime("%Y-%m-%d")
        log_filename = f"user_actions_{today}_{current_shift}.log"
        log_filepath = os.path.join(ACTION_LOG_FOLDER, log_filename)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - User: {username} - Action: {action}\n"
        with open(log_filepath, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        logging.error(f"Failed to log user action: {e}")

def get_mosaic_config_path():
    return os.path.join(BASE_CONFIG_PATH, "mosaic_settings.json")

def load_mosaic_config():
    default_config = {
        "overview": {
            "lateral": { "orientation": 0, "layout": "horizontal", "max_images": 8, "active_machines": {} },
            "fundo": { "orientation": 0, "layout": "horizontal", "max_images": 8, "active_machines": {} }
        },
        "xml_config": {
            "available_xml_fields": [],
            "selectedFields": { "timestamp": "Timestamp", "result": "Result", "defect_type": "Defect Type", "confidence": "Confidence" }
        },
        "display_config": {
            "orientation": 0, "grid_columns": 4, "grid_lines": 3, "image_size": 300,
            "max_images": 50, "refresh_interval": 30, "zoom_percentage": 250
        },
        "overlay_config": { "top": "NOM_ART", "bottom_left": "NUM_MOULE", "bottom_right": "DATE" },
        "filter_config": { "NUM_CAM": { "enabled": True, "available_values": [], "selected_values": [] } }
    }
    
    mosaic_file = get_mosaic_config_path()
    config = safe_load_json(mosaic_file, default_config)
    
    if config is not default_config:
        for key, value in default_config.items():
            if key not in config:
                config[key] = value
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if sub_key not in config[key]:
                        config[key][sub_key] = sub_value
    return config

last_shift_reset = get_current_shift()

# ==============================================================================
# TEMPLATES HTML 
# ==============================================================================

LOGIN_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
    <meta charset="UTF-8">
    <title>{{ t('Login - Sistema de Gestão') }}</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-container { background: rgba(255, 255, 255, 0.95); padding: 2.5rem 3rem; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); width: 100%; max-width: 400px; text-align: center; }
        .logo { width: 180px; height: auto; margin: 0 auto 1.5rem auto; display: block; }
        .login-container h1 { color: #2c3e50; margin-bottom: 1.5rem; font-size: 1.8rem; }
        .form-group { margin-bottom: 1.5rem; text-align: left; }
        .form-group label { display: block; margin-bottom: 0.5rem; font-weight: 600; color: #495057; }
        .form-control { width: 100%; padding: 0.8rem; border: 1px solid #ced4da; border-radius: 6px; font-size: 1rem; box-sizing: border-box; }
        .btn { width: 100%; padding: 0.9rem; border: none; border-radius: 6px; background: #667eea; color: white; font-size: 1.1rem; font-weight: 600; cursor: pointer; transition: background 0.3s ease; }
        .btn:hover { background: #5a6fd6; }
        .error-message { color: #e74c3c; background: #f8d7da; border: 1px solid #f5c6cb; padding: 0.8rem; border-radius: 6px; margin-top: 1.5rem; }
        .lang-selector { display: flex; justify-content: center; gap: 15px; margin-bottom: 20px; }
        .lang-selector label { cursor: pointer; display: flex; align-items: center; font-size: 1.5rem; }
        .lang-selector input[type="radio"] { display: none; }
        .lang-selector input[type="radio"]:checked + span { border-bottom: 2px solid #667eea; transform: scale(1.1); }
        .lang-selector span { padding: 2px; transition: all 0.2s ease; }
    </style>
</head>
<body>
    <div class="login-container">
        {% if logo_data %}
            <img src="data:image/png;base64,{{ logo_data }}" alt="Logo da Empresa" class="logo">
        {% endif %}
        
        <div class="lang-selector">
            <label><input type="radio" name="ui_lang" value="pt" onclick="changeLang('pt')" {% if lang == 'pt' %}checked{% endif %}><span>🇵🇹</span></label>
            <label><input type="radio" name="ui_lang" value="es" onclick="changeLang('es')" {% if lang == 'es' %}checked{% endif %}><span>🇪🇸</span></label>
            <label><input type="radio" name="ui_lang" value="en" onclick="changeLang('en')" {% if lang == 'en' %}checked{% endif %}><span>🇬🇧</span></label>
            <label><input type="radio" name="ui_lang" value="pl" onclick="changeLang('pl')" {% if lang == 'pl' %}checked{% endif %}><span>🇵🇱</span></label>
            <label><input type="radio" name="ui_lang" value="bg" onclick="changeLang('bg')" {% if lang == 'bg' %}checked{% endif %}><span>🇧🇬</span></label>
        </div>

        <h1>{{ t('Gestão de Backups') }}</h1>
        <form method="post">
            <div class="form-group">
                <label>{{ t('Utilizador') }}</label>
                <input type="text" name="username" class="form-control" required>
            </div>
            <div class="form-group">
                <label>{{ t('Password') }}</label>
                <input type="password" name="password" class="form-control" required>
            </div>
            <button type="submit" class="btn">{{ t('Entrar no Painel') }}</button>
        </form>
        <div style="margin-top:20px;">
            <a href="/historico_externo" style="color: #667eea; text-decoration: none; font-weight: bold;">&#8594; {{ t('Aceder ao Portal Público de Histórico') }}</a>
        </div>
        <div style="margin-top:10px;">
            <a href="http://{{ request.host.split(':')[0] }}:5582" style="color: #8e44ad; text-decoration: none; font-weight: bold;">&#8594; {{ t('Portal Criação Pen PKIRIS') }}</a>
        </div>
        {% if error %}<p class="error-message">{{ t(error) }}</p>{% endif %}
    </div>

    <script>
    function changeLang(lang) {
        fetch('/set_lang', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang: lang })
        }).then(() => window.location.reload());
    }
    </script>
</body>
</html>
"""

BACKUP_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
<meta charset="UTF-8">
<title>{{ t('Sistema de Gestão de Imagens - Backup Avançado') }}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer">
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; color: #333; }
    .header { background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); padding: 1.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.1); position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; }
    .header-title { text-align: center; flex-grow: 1; }
    .header h1 { color: #2c3e50; font-size: 2rem; margin-bottom: 0.5rem; }
    .header p { color: #7f8c8d; font-size: 1rem; }
    .user-info { text-align: right; }
    .user-info span { display: block; font-weight: bold; color: #2c3e50; }
    .container { max-width: 1600px; margin: 0 auto; padding: 2rem; }
    .tabs { display: flex; background: rgba(255,255,255,0.9); border-radius: 12px 12px 0 0; margin-bottom: 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); flex-wrap: wrap; }
    .tab { flex: 1; padding: 1rem 2rem; background: transparent; border: none; cursor: pointer; font-size: 1rem; font-weight: 600; color: #7f8c8d; transition: all 0.3s ease; border-bottom: 3px solid transparent; white-space: nowrap; }
    .tab:hover { background: rgba(103, 126, 234, 0.1); color: #667eea; }
    .tab.active { color: #667eea; border-bottom-color: #667eea; background: rgba(103, 126, 234, 0.1); }
    .tab-content { background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); border-radius: 0 0 12px 12px; padding: 2rem; box-shadow: 0 4px 20px rgba(0,0,0,0.1); display: none; }
    .tab-content.active { display: block; }
    .section { background: white; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 2px 15px rgba(0,0,0,0.1); border-left: 4px solid #667eea; }
    .section h3 { color: #2c3e50; margin-bottom: 1rem; font-size: 1.3rem; display: flex; align-items: center; gap: 0.5rem; }
    .section h4 { color: #2c3e50; margin-top: 1.5rem; margin-bottom: 0.75rem; padding-bottom: 0.5rem; border-bottom: 2px solid #eee; font-size: 1.1rem; }
    .linha-card { background: white; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 2px 15px rgba(0,0,0,0.1); border-left: 4px solid #e74c3c; }
    .linha-card h4 { color: #e74c3c; margin-bottom: 1rem; font-size: 1.2rem; text-align: center; background: rgba(231, 76, 60, 0.1); padding: 0.5rem; border-radius: 8px; }
    .machine-section, .ramal { background: #f8f9fa; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; border: 1px solid #dee2e6; }
    .machine-section h5, .ramal h5 { color: #495057; margin-bottom: 0.75rem; font-size: 1rem; border-bottom: 2px solid #dee2e6; padding-bottom: 0.5rem; }
    .machine-section h6, .ramal h6 { color: #6c757d; margin-bottom: 0.5rem; font-size: 0.9rem; }
    .form-group { margin-bottom: 1rem; }
    .form-group label { display: block; margin-bottom: 0.5rem; font-weight: 600; color: #495057; }
    .form-control { width: 100%; padding: 0.75rem; border: 1px solid #ced4da; border-radius: 6px; font-size: 0.9rem; transition: border-color 0.3s ease; }
    .form-control:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(103, 126, 234, 0.1); }
    .path-input-group { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.75rem; }
    .path-input-group label { min-width: 120px; margin-bottom: 0; font-size: 0.9rem; }
    .path-input-group input { flex: 1; padding: 0.5rem; border: 1px solid #ced4da; border-radius: 4px; }
    .time-input-group { display: grid; grid-template-columns: 120px 1fr 1fr 1fr; gap: 0.5rem; align-items: center; margin-bottom: 0.75rem; }
    .time-input-group label { font-size: 0.9rem; font-weight: 600; }
    .time-input-group input { padding: 0.5rem; border: 1px solid #ced4da; border-radius: 4px; }
    .browse-btn { padding: 0.5rem 1rem; background: #17a2b8; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 0.8rem; display: flex; align-items: center; gap: 0.25rem; }
    .browse-btn:hover { background: #138496; }
    .status-led { width: 16px; height: 16px; border-radius: 50%; background: #dc3545; display: inline-block; margin-left: 0.5rem; position: relative; transition: all 0.3s ease; }
    .status-led.online { background: #28a745; box-shadow: 0 0 8px rgba(40, 167, 69, 0.5); }
    .status-led.checking { background: #ffc107; animation: pulse 1.5s infinite; }
    .service-led { width: 12px; height: 12px; border-radius: 50%; background: #dc3545; display: inline-block; margin-left: 0.5rem; position: relative; transition: all 0.3s ease; }
    .service-led.active { background: #28a745; box-shadow: 0 0 6px rgba(40, 167, 69, 0.8); }
    .service-led.inactive { background: #dc3545; }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
    .btn { padding: 0.75rem 1.5rem; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 0.5rem; transition: all 0.3s ease; font-size: 0.9rem; margin: 0.25rem; }
    .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.2); }
    .btn-primary { background: #667eea; color: white; }
    .btn-success { background: #28a745; color: white; }
    .btn-warning { background: #ffc107; color: #212529; }
    .btn-danger { background: #dc3545; color: white; }
    .btn-info { background: #17a2b8; color: white; }
    .btn-sm { padding: 0.5rem 1rem; font-size: 0.8rem; }
    .status-indicator { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.5rem 1rem; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }
    .status-running { background: #d4edda; color: #155724; }
    .status-stopped { background: #f8d7da; color: #721c24; }
    .status-configured { background: #d1ecf1; color: #0c5460; }
    .machine-buttons { display: flex; gap: 0.5rem; margin-top: 0.5rem; flex-wrap: wrap; align-items: center; }
    .ramal-container { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
    .stats-card { background: white; padding: 1.5rem; border-radius: 12px; text-align: center; box-shadow: 0 2px 15px rgba(0,0,0,0.1); border-left: 4px solid #28a745; }
    .stats-value { font-size: 2rem; font-weight: bold; color: #28a745; display: block; }
    .stats-label { color: #6c757d; font-size: 0.9rem; margin-top: 0.5rem; }
    
    .progress-container { width: 100%; margin-top: 15px; }
    .log-window { background: #1e1e1e; color: #2ecc71; font-family: monospace; font-size: 0.85rem; height: 180px; overflow-y: auto; padding: 10px; border-radius: 6px; margin-bottom: 10px; box-shadow: inset 0 0 10px rgba(0,0,0,0.5); }
    .log-window p { margin: 2px 0; }
    .log-current-file { color: #f1c40f; margin-bottom: 10px; font-family: monospace; font-size: 0.85rem; word-break: break-all; }
    .progress { background: #e9ecef; border-radius: 0.5rem; height: 2.5rem; overflow: hidden; position: relative; box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
    .progress-bar { background: linear-gradient(45deg, #667eea, #764ba2); height: 100%; transition: width 0.4s ease; width: 0%; position: absolute; top: 0; left: 0; z-index: 1; }
    .progress-text { position: absolute; width: 100%; height: 100%; top: 0; left: 0; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 1.1rem; text-shadow: 1px 1px 3px rgba(0,0,0,0.9), -1px -1px 3px rgba(0,0,0,0.9); z-index: 2; pointer-events: none; }
    
    .checkbox-group { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.75rem; }
    .checkbox-group input[type="checkbox"] { width: auto; margin: 0; }
    .export-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; margin-bottom: 1rem; }
    .export-section h4 { margin-bottom: 0.5rem; color: #495057; font-size: 1rem; }
    .checkbox-list { max-height: 250px; overflow-y: auto; background: #f8f9fa; padding: 1rem; border-radius: 8px; border: 1px solid #ced4da; margin-top: 1rem; }
    .checkbox-item { display: flex; align-items: center; gap: 0.5rem; padding: 0.25rem 0; }
    .ip-list { background: #f8f9fa; padding: 1rem; border-radius: 8px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.9rem; }
    .ip-item { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem; border-bottom: 1px solid #dee2e6; }
    .ip-status { padding: 0.2rem 0.5rem; border-radius: 12px; font-size: 0.8rem; font-weight: bold; }
    .ip-status.online { background: #d4edda; color: #155724; }
    .ip-status.offline { background: #f8d7da; color: #721c24; }
    .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); }
    .modal-content { background-color: white; margin: 5% auto; padding: 0; border-radius: 12px; width: 80%; max-width: 800px; max-height: 80%; overflow: hidden; }
    .modal-header { background: #667eea; color: white; padding: 1rem; display: flex; justify-content: space-between; align-items: center; }
    .modal-header h3 { margin: 0; flex: 1; }
    .close { color: white; font-size: 1.5rem; font-weight: bold; cursor: pointer; background: none; border: none; }
    .close:hover { opacity: 0.7; }
    .modal-body { padding: 1rem; max-height: 400px; overflow-y: auto; }
    .file-list { list-style: none; padding: 0; }
    .file-item { padding: 0.5rem; border-bottom: 1px solid #eee; cursor: pointer; display: flex; align-items: center; gap: 0.5rem; }
    .file-item:hover { background: #f8f9fa; }
    .file-item.folder { color: #667eea; }
    .file-item.parent { color: #6c757d; font-style: italic; }
    .current-path { background: #f8f9fa; padding: 0.5rem; border-radius: 4px; margin-bottom: 1rem; font-family: monospace; }
    .modal-footer { padding: 1rem; border-top: 1px solid #eee; display: flex; gap: 1rem; justify-content: flex-end; }
    .message-container { position: fixed; top: 0; left: 0; right: 0; z-index: 1000; pointer-events: none; padding: 1rem; }
    .alert { padding: 1rem; border-radius: 6px; margin-bottom: 1rem; border: 1px solid transparent; pointer-events: auto; box-shadow: 0 4px 12px rgba(0,0,0,0.15); backdrop-filter: blur(10px); animation: slideDown 0.3s ease-out; }
    @keyframes slideDown { from { transform: translateY(-100%); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
    .alert-success { background: rgba(212, 237, 218, 0.95); color: #155724; border-color: #c3e6cb; }
    .alert-danger { background: rgba(248, 215, 218, 0.95); color: #721c24; border-color: #f5c6cb; }
    .alert-info { background: rgba(209, 236, 241, 0.95); color: #0c5460; border-color: #bee5eb; }
    .switch { position: relative; display: inline-block; width: 50px; height: 24px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #ccc; transition: .4s; border-radius: 24px; }
    .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .4s; border-radius: 50%; }
    input:checked + .slider { background-color: #4CAF50; }
    input:checked + .slider:before { transform: translateX(26px); }
    .switch-test input:checked + .slider { background-color: #e67e22; }
    .toggle-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
    .toggle-item:last-child { border-bottom: none; }
    .toggle-item span { font-weight: 500; color: #2c3e50; }
    .service-table, .user-table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
    .service-table th, .service-table td, .user-table th, .user-table td { border: 1px solid #dee2e6; padding: 0.75rem; text-align: left; vertical-align: middle; }
    .service-table th, .user-table th { background: #e9ecef; font-weight: 600; }
    .service-table .actions, .user-table .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
    .checkbox-item-alias { display: grid; grid-template-columns: 20px 1fr 1fr; gap: 0.75rem; align-items: center; margin-bottom: 0.5rem; }
    .checkbox-item input[type="checkbox"] { width: auto; }
    .grid-2-col { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }
    @media (max-width: 992px) { .grid-2-col { grid-template-columns: 1fr; } }
    @media (max-width: 768px) { .tabs { flex-direction: column; } .path-input-group { flex-direction: column; align-items: flex-start; } .path-input-group label { min-width: auto; } .ramal-container { grid-template-columns: 1fr; } .modal-content { width: 95%; margin: 2% auto; } .time-input-group { grid-template-columns: 1fr; } .export-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<div class="header">
    <div class="header-title">
        <h1><i class="fas fa-cogs"></i> {{ t('Sistema de Gestão de Imagens') }}</h1>
        <p>{{ t('Sistema Avançado com Turnos, Mosaicos 4K e Modo Teste Dinâmico') }}</p>
    </div>
    <div class="user-info">
        <span><i class="fas fa-user"></i> {{ session.username }}</span>
        <a href="{{ url_for('logout') }}" class="btn btn-sm btn-danger">
            <i class="fas fa-sign-out-alt"></i> {{ t('Sair') }}
        </a>
        <br>
        <a href="/historico_externo" target="_blank" style="text-decoration: none; color: #667eea; font-size: 0.9rem; font-weight: bold; margin-top: 5px; display: inline-block;">
            <i class="fas fa-external-link-alt"></i> {{ t('Portal Histórico Público') }}
        </a>
    </div>
</div>

<div class="container">
    <div class="tabs">
        <button class="tab active" onclick="showTab('configuracao')"><i class="fas fa-cog"></i> {{ t('Configuração') }}</button>
        <button class="tab" onclick="showTab('backup_pkiris')"><i class="fas fa-file-archive"></i> {{ t('Backup PKIRIS') }}</button>
        <button class="tab" onclick="showTab('backup_historicos')"><i class="fas fa-history"></i> {{ t('Backup Históricos') }}</button>
        <button class="tab" onclick="showTab('backup_artigos')"><i class="fas fa-box-open"></i> {{ t('Backup Artigos') }}</button>
        <button class="tab" onclick="showTab('servicos')"><i class="fas fa-server"></i> {{ t('Gestão de Serviços') }}</button>
        <button class="tab" onclick="showTab('exportar')"><i class="fas fa-download"></i> {{ t('Exportar') }}</button>
        <button class="tab" onclick="showTab('diagnostics')"><i class="fas fa-chart-line"></i> {{ t('Diagnóstico') }}</button>
        <button class="tab" onclick="showTab('gestao_mosaico')"><i class="fas fa-tasks"></i> {{ t('Gestão Mosaico') }}</button>
        <button class="tab" onclick="showTab('config_mosaico')"><i class="fas fa-th"></i> {{ t('Config Mosaico') }}</button>
        <button class="tab" onclick="showTab('users')"><i class="fas fa-users"></i> {{ t('Utilizadores') }}</button>
        <button class="tab" onclick="showTab('logs')"><i class="fas fa-file-alt"></i> {{ t('Logs') }}</button>
    </div>

    <div id="configuracao" class="tab-content active">
        <div class="section">
            <h3><i class="fas fa-sliders-h"></i> {{ t('Configuração Geral') }}</h3>
            <form id="configGeralForm">
                <div class="path-input-group">
                    <label>{{ t('SSD Path:') }}</label>
                    <input type="text" name="ssd_path" id="ssd_path" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('ssd_path')"><i class="fas fa-folder-open"></i></button>
                    <span class="status-led" id="led_ssd_path"></span>
                </div>
                <div class="path-input-group">
                    <label>{{ t('Pasta para Mirror SSD:') }}</label>
                    <input type="text" name="mirror_source_path" id="mirror_source_path" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('mirror_source_path')"><i class="fas fa-folder-open"></i></button>
                    <span class="status-led" id="led_mirror_source_path"></span>
                </div>
                <div class="path-input-group" style="display: none;">
                    <label>{{ t('Pasta Config Mosaico (OBSOLETO):') }}</label>
                    <input type="text" name="mosaic_config_folder" id="mosaic_config_folder" class="form-control">
                </div>
                <div class="path-input-group">
                    <label>{{ t('Caminho Ficheiro de Log:') }}</label>
                    <input type="text" name="log_file_path" id="log_file_path" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('log_file_path')"><i class="fas fa-file-alt"></i></button>
                    <span class="status-led" id="led_log_file_path"></span>
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="mirror_include_subfolders" id="mirror_include_subfolders">
                    <label for="mirror_include_subfolders">{{ t('Incluir subpastas no mirror SSD') }}</label>
                </div>
                <div class="path-input-group">
                    <label>{{ t('Retenção SSD (dias):') }}</label>
                    <input type="number" name="ssd_retention_days" id="ssd_retention_days" class="form-control">
                </div>
                <div class="path-input-group">
                    <label>{{ t('Retenção HDD (meses):') }}</label>
                    <input type="number" name="hdd_retention_months" id="hdd_retention_months" class="form-control">
                </div>
                <div class="path-input-group">
                    <label>{{ t('Intervalo verificação (seg):') }}</label>
                    <input type="number" name="scan_interval_sec" id="scan_interval_sec" class="form-control">
                </div>
                <h4><i class="fas fa-clock"></i> {{ t('Configuração de Turnos') }}</h4>
                <div class="time-input-group">
                    <label>{{ t('1º Turno:') }}</label>
                    <input type="time" name="turno1_inicio" id="turno1_inicio" class="form-control">
                    <input type="time" name="turno1_fim" id="turno1_fim" class="form-control">
                    <span>{{ t('Início - Fim') }}</span>
                </div>
                <div class="time-input-group">
                    <label>{{ t('2º Turno:') }}</label>
                    <input type="time" name="turno2_inicio" id="turno2_inicio" class="form-control">
                    <input type="time" name="turno2_fim" id="turno2_fim" class="form-control">
                    <span>{{ t('Início - Fim') }}</span>
                </div>
                <div class="time-input-group">
                    <label>{{ t('3º Turno:') }}</label>
                    <input type="time" name="turno3_inicio" id="turno3_inicio" class="form-control">
                    <input type="time" name="turno3_fim" id="turno3_fim" class="form-control">
                    <span>{{ t('Início - Fim') }}</span>
                </div>
                <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> {{ t('Gravar Configuração Geral') }}</button>
            </form>
        </div>
        
        <div class="section" style="border-left-color: #f1c40f;">
            <h3><i class="fas fa-search"></i> {{ t('Análise Global de Artigos (JSON) - Retroativa') }}</h3>
            <p>{{ t('O sistema já atualiza os artigos automaticamente durante as cópias em tempo real. Use esta ferramenta apenas se precisar de forçar a leitura de pastas históricas antigas não registadas.') }}</p>
            <div class="path-input-group">
                <label style="min-width: 150px; font-weight:bold; color: #d35400;">{{ t('Pasta Raiz (Obrigatório):') }}</label>
                <input type="text" id="article_analysis_path" name="article_analysis_path" class="form-control" placeholder="{{ t('Aponte para a pasta origem na NAS. (ex: /volume1/inspecao)') }}" onchange="checkPathAccess(this.id)">
                <button type="button" class="browse-btn" onclick="openFileBrowser('article_analysis_path')"><i class="fas fa-folder-open"></i></button>
                <span class="status-led" id="led_article_analysis_path"></span>
            </div>
            <div style="margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap;">
                <button type="button" class="btn btn-primary" onclick="startArticleAnalysis()"><i class="fas fa-play"></i> {{ t('Iniciar Análise') }}</button>
                <button type="button" class="btn btn-warning" onclick="resetArticleAnalysis()"><i class="fas fa-redo"></i> {{ t('Forçar Reset (Nova Análise)') }}</button>
                <button type="button" class="btn btn-danger" onclick="stopArticleAnalysis()"><i class="fas fa-stop"></i> {{ t('Parar Análise') }}</button>
            </div>
            <div id="articleAnalysisProgress" style="display: none; margin-top: 15px;">
                <div style="display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 5px;">
                    <span id="aa_status_text" style="color: #3498db;">{{ t('A iniciar...') }}</span>
                    <span id="aa_eta_text" style="color: #e74c3c;">{{ t('ETA: --:--') }}</span>
                </div>
                
                <div class="log-window" id="aa_log_window"></div>
                <div class="log-current-file" id="aa_current_file">{{ t('Ficheiro atual: ...') }}</div>
                
                <div class="progress-container">
                    <div class="progress">
                        <div class="progress-bar" id="aa_progress_bar" style="width: 0%"></div>
                        <div class="progress-text" id="aa_progress_text">{{ t('A preparar...') }}</div>
                    </div>
                </div>
            </div>
        </div>
        
        {% for linha in ['21', '22', '23', '24', '31', '32', '33'] %}
        <div class="linha-card" id="linha-card-{{ linha }}">
            <h4><i class="fas fa-industry"></i> {{ t('Linha') }} {{ linha }}</h4>
            <div class="machine-section" style="background: #f0f4ff; border-color: #667eea;">
                <h5><i class="fas fa-sync-alt"></i> {{ t('Configurações da Linha') }} {{ linha }}</h5>
                <div class="path-input-group">
                    <label>{{ t('Ativar Modo Ciclo:') }}</label>
                    <label class="switch"><input type="checkbox" name="cycle_mode_active_{{ linha }}" id="cycle_mode_active_{{ linha }}"><span class="slider"></span></label>
                </div>
                <div class="path-input-group">
                    <label>{{ t('Tempo de Ciclo (seg):') }}</label>
                    <input type="number" name="cycle_time_sec_{{ linha }}" id="cycle_time_sec_{{ linha }}" class="form-control" style="max-width: 150px;">
                </div>
                <div class="path-input-group" style="margin-top: 15px; border-top: 1px solid #c3d0ff; padding-top: 10px;">
                    <label style="color: #e67e22; min-width: 250px;"><i class="fas fa-vial"></i> {{ t('Ativar Modo Teste (Caminhos Alt.):') }}</label>
                    <label class="switch switch-test"><input type="checkbox" name="use_test_mode_{{ linha }}" id="use_test_mode_{{ linha }}"><span class="slider"></span></label>
                    <span style="font-size: 0.85rem; color: #666; margin-left: 10px;">{{ t('Força os serviços a usar os Caminhos de Teste.') }}</span>
                </div>
            </div>

            {% for maquina in ['lateral', 'fundo'] %}
            <div class="machine-section">
                <h5><i class="fas fa-camera"></i> {{ t('Lateral') if maquina == 'lateral' else t('Topo e Fundo') }}</h5>
                
                <div style="background: #f8f9fa; border-left: 3px solid #3498db; padding: 10px; margin-bottom: 10px; border-radius: 4px;">
                    <h6 style="margin-top: 0; margin-bottom: 10px; color: #3498db;"><i class="fas fa-server"></i> {{ t('Caminhos de Produção') }}</h6>
                    <div class="path-input-group">
                        <label>{{ t('Origem (PROD):') }}</label>
                        <input type="text" name="linhas[{{ linha }}][{{ maquina }}][src_prod]" id="origem_prod_{{ linha }}_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                        <button type="button" class="browse-btn" onclick="openFileBrowser('origem_prod_{{ linha }}_{{ maquina }}')"><i class="fas fa-network-wired"></i></button>
                        <span class="status-led" id="led_origem_prod_{{ linha }}_{{ maquina }}"></span>
                    </div>
                    <div class="path-input-group">
                        <label>{{ t('Destino (PROD):') }}</label>
                        <input type="text" name="linhas[{{ linha }}][{{ maquina }}][dst_prod]" id="destino_prod_{{ linha }}_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                        <button type="button" class="browse-btn" onclick="openFileBrowser('destino_prod_{{ linha }}_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                        <span class="status-led" id="led_destino_prod_{{ linha }}_{{ maquina }}"></span>
                    </div>
                </div>
                
                <div style="background: #fffcf5; border-left: 3px solid #f39c12; padding: 10px; margin-bottom: 10px; border-radius: 4px;">
                    <h6 style="margin-top: 0; margin-bottom: 10px; color: #f39c12;"><i class="fas fa-flask"></i> {{ t('Caminhos de Teste') }}</h6>
                    <div class="path-input-group">
                        <label>{{ t('Origem (TESTE):') }}</label>
                        <input type="text" name="linhas[{{ linha }}][{{ maquina }}][src_test]" id="origem_test_{{ linha }}_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                        <button type="button" class="browse-btn" onclick="openFileBrowser('origem_test_{{ linha }}_{{ maquina }}')"><i class="fas fa-network-wired"></i></button>
                        <span class="status-led" id="led_origem_test_{{ linha }}_{{ maquina }}"></span>
                    </div>
                    <div class="path-input-group">
                        <label>{{ t('Destino (TESTE):') }}</label>
                        <input type="text" name="linhas[{{ linha }}][{{ maquina }}][dst_test]" id="destino_test_{{ linha }}_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                        <button type="button" class="browse-btn" onclick="openFileBrowser('destino_test_{{ linha }}_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                        <span class="status-led" id="led_destino_test_{{ linha }}_{{ maquina }}"></span>
                    </div>
                </div>
                
                <div class="path-input-group">
                    <label>{{ t('Porta Mosaico:') }}</label>
                    <input type="number" name="linhas[{{ linha }}][{{ maquina }}][mosaic_port]" id="mosaic_port_{{ linha }}_{{ maquina }}" class="form-control">
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="linhas[{{ linha }}][{{ maquina }}][delete_source]" id="delete_source_{{ linha }}_{{ maquina }}">
                    <label for="delete_source_{{ linha }}_{{ maquina }}">{{ t('Excluir arquivo origem após cópia') }}</label>
                </div>
                <div class="machine-buttons">
                    <button type="button" class="btn btn-primary btn-sm" onclick="toggleBackup('{{ linha }}', '{{ maquina }}')"><i class="fas fa-play"></i> {{ t('Backup') }}</button>
                    <span class="service-led" id="service_backup_{{ linha }}_{{ maquina }}"></span>
                    <button type="button" class="btn btn-info btn-sm" onclick="toggleMosaico('{{ linha }}', '{{ maquina }}')"><i class="fas fa-th"></i> {{ t('Mosaico') }}</button>
                    <span class="service-led" id="service_mosaic_{{ linha }}_{{ maquina }}"></span>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endfor %}

        <div class="linha-card" id="linha-card-34">
            <h4><i class="fas fa-industry"></i> {{ t('Linha 34 - Configuração Especial') }}</h4>
            <div class="machine-section" style="background: #f0f4ff; border-color: #667eea;">
                <h5><i class="fas fa-sync-alt"></i> {{ t('Configurações da Linha 34') }}</h5>
                <div class="path-input-group">
                    <label>{{ t('Ativar Modo Ciclo:') }}</label>
                    <label class="switch"><input type="checkbox" name="cycle_mode_active_34" id="cycle_mode_active_34"><span class="slider"></span></label>
                </div>
                <div class="path-input-group">
                    <label>{{ t('Tempo de Ciclo (seg):') }}</label>
                    <input type="number" name="cycle_time_sec_34" id="cycle_time_sec_34" class="form-control" style="max-width: 150px;">
                </div>
                <div class="path-input-group" style="margin-top: 15px; border-top: 1px solid #c3d0ff; padding-top: 10px;">
                    <label style="color: #e67e22; min-width: 250px;"><i class="fas fa-vial"></i> {{ t('Ativar Modo Teste (Caminhos Alt.):') }}</label>
                    <label class="switch switch-test"><input type="checkbox" name="use_test_mode_34" id="use_test_mode_34"><span class="slider"></span></label>
                </div>
            </div>

            <div class="ramal-container">
                {% for ramal in [1, 2] %}
                <div class="ramal">
                    <h5><i class="fas fa-sitemap"></i> {{ t('Ramal') }} {{ ramal }}</h5>
                    {% for maq in ['lateral', 'fundo'] %}
                    {% set maquina = maq ~ ramal %}
                    <div class="machine-section">
                        <h6><i class="fas fa-camera"></i> {{ t('Lateral') if maq == 'lateral' else t('Fundo') }} {{ ramal }}</h6>
                        
                        <div style="background: #f8f9fa; border-left: 3px solid #3498db; padding: 10px; margin-bottom: 10px; border-radius: 4px;">
                            <h6 style="margin-top: 0; margin-bottom: 10px; color: #3498db;">{{ t('Caminhos de Produção') }}</h6>
                            <div class="path-input-group">
                                <label>{{ t('Origem (PROD):') }}</label>
                                <input type="text" name="linhas[34][{{ maquina }}][src_prod]" id="origem_prod_34_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('origem_prod_34_{{ maquina }}')"><i class="fas fa-network-wired"></i></button>
                                <span class="status-led" id="led_origem_prod_34_{{ maquina }}"></span>
                            </div>
                            <div class="path-input-group">
                                <label>{{ t('Destino (PROD):') }}</label>
                                <input type="text" name="linhas[34][{{ maquina }}][dst_prod]" id="destino_prod_34_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('destino_prod_34_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                                <span class="status-led" id="led_destino_prod_34_{{ maquina }}"></span>
                            </div>
                        </div>
                        
                        <div style="background: #fffcf5; border-left: 3px solid #f39c12; padding: 10px; margin-bottom: 10px; border-radius: 4px;">
                            <h6 style="margin-top: 0; margin-bottom: 10px; color: #f39c12;">{{ t('Caminhos de Teste') }}</h6>
                            <div class="path-input-group">
                                <label>{{ t('Origem (TESTE):') }}</label>
                                <input type="text" name="linhas[34][{{ maquina }}][src_test]" id="origem_test_34_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('origem_test_34_{{ maquina }}')"><i class="fas fa-network-wired"></i></button>
                                <span class="status-led" id="led_origem_test_34_{{ maquina }}"></span>
                            </div>
                            <div class="path-input-group">
                                <label>{{ t('Destino (TESTE):') }}</label>
                                <input type="text" name="linhas[34][{{ maquina }}][dst_test]" id="destino_test_34_{{ maquina }}" class="form-control" onchange="checkPathAccess(this.id)">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('destino_test_34_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                                <span class="status-led" id="led_destino_test_34_{{ maquina }}"></span>
                            </div>
                        </div>

                        <div class="path-input-group">
                            <label>{{ t('Porta Mosaico:') }}</label>
                            <input type="number" name="linhas[34][{{ maquina }}][mosaic_port]" id="mosaic_port_34_{{ maquina }}" class="form-control">
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" name="linhas[34][{{ maquina }}][delete_source]" id="delete_source_34_{{ maquina }}">
                            <label for="delete_source_34_{{ maquina }}">{{ t('Excluir arquivo origem') }}</label>
                        </div>
                        <div class="machine-buttons">
                            <button type="button" class="btn btn-primary btn-sm" onclick="toggleBackup('34', '{{ maquina }}')"><i class="fas fa-play"></i> {{ t('Backup') }}</button>
                            <span class="service-led" id="service_backup_34_{{ maquina }}"></span>
                            <button type="button" class="btn btn-info btn-sm" onclick="toggleMosaico('34', '{{ maquina }}')"><i class="fas fa-th"></i> {{ t('Mosaico') }}</button>
                            <span class="service-led" id="service_mosaic_34_{{ maquina }}"></span>
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% endfor %}
            </div>
        </div>

        <div class="linha-card" id="linha-card-global" style="border-left-color: #f39c12;">
            <h4 style="color: #f39c12; background: rgba(243, 156, 18, 0.1);"><i class="fas fa-globe"></i> {{ t('Visão Global (Dashboard 4K)') }}</h4>
            <div class="machine-section" style="background: #fffcf5; border-color: #f39c12;">
                <h5><i class="fas fa-tv"></i> {{ t('Configuração dos Servidores Overview') }}</h5>
                <div class="path-input-group"><label>{{ t('Porta Lateral:') }}</label><input type="number" id="overview_port_lateral" class="form-control" style="max-width: 150px;"></div>
                <div class="path-input-group"><label>{{ t('Porta Fundo:') }}</label><input type="number" id="overview_port_fundo" class="form-control" style="max-width: 150px;"></div>
                <div class="path-input-group"><label>{{ t('Ativar Ciclo Auto:') }}</label><label class="switch"><input type="checkbox" id="overview_cycle_mode_active"><span class="slider"></span></label></div>
                <div class="path-input-group"><label>{{ t('Tempo de Ciclo (seg):') }}</label><input type="number" id="overview_cycle_time_sec" class="form-control" style="max-width: 150px;"></div>
                <div class="machine-buttons" style="margin-top: 15px;">
                    <button type="button" class="btn btn-warning btn-sm" onclick="toggleMosaico('Global', 'lateral')"><i class="fas fa-power-off"></i> {{ t('Ativar Dashboard Lateral') }}</button>
                    <span class="service-led" id="service_mosaic_Global_lateral" title="{{ t('Status do Dashboard Lateral') }}"></span>
                    <button type="button" class="btn btn-warning btn-sm" onclick="toggleMosaico('Global', 'fundo')"><i class="fas fa-power-off"></i> {{ t('Ativar Dashboard Fundo') }}</button>
                    <span class="service-led" id="service_mosaic_Global_fundo" title="{{ t('Status do Dashboard Fundo') }}"></span>
                </div>
            </div>
        </div>

        <div class="section">
            <button type="button" class="btn btn-success" onclick="saveAllConfig()"><i class="fas fa-save"></i> {{ t('Gravar Todas as Configurações') }}</button>
            <button type="button" class="btn btn-info" onclick="checkAllPaths()"><i class="fas fa-check-circle"></i> {{ t('Verificar Caminhos') }}</button>
            <button type="button" class="btn btn-warning" onclick="startFileCopying()"><i class="fas fa-copy"></i> {{ t('Iniciar Cópia') }}</button>
            <span class="service-led" id="service_file_copying" title="{{ t('Status do serviço de cópia') }}"></span>
            <button type="button" class="btn btn-info" onclick="startMirrorSSD()"><i class="fas fa-hdd"></i> {{ t('Iniciar Mirror SSD') }}</button>
            <span class="service-led" id="service_mirror_ssd"></span>
            <button type="button" class="btn btn-danger" onclick="stopAllServices()"><i class="fas fa-stop"></i> {{ t('Parar Todos os Serviços') }}</button>
        </div>
    </div>
    
    <div id="backup_pkiris" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-file-archive"></i> {{ t('Configuração Global Backup PKIRIS') }}</h3>
            <p>{{ t('Define a pasta base de destino. O sistema irá criar automaticamente subpastas para cada Linha e Máquina (/Linha_XX/Lateral).') }}</p>
            <form id="pkirisConfigForm">
                <div class="path-input-group">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Retenção PKIRIS (dias):') }}</label>
                    <input type="number" name="pkiris_retention_days" id="pkiris_retention_days" class="form-control" style="max-width: 150px;">
                </div>
                <div class="path-input-group" style="margin-bottom: 20px;">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Pasta Destino Raiz PKIRIS:') }}</label>
                    <input type="text" name="pkiris_dst_root" id="pkiris_dst_root" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('pkiris_dst_root')"><i class="fas fa-folder-open"></i></button>
                </div>
                
                <h4 style="margin-top:20px; border-bottom:2px solid #ccc; padding-bottom:5px; color: #2c3e50;"><i class="fas fa-sitemap"></i> {{ t('Diretorias de Origem (Onde a máquina gera os .pkiris)') }}</h4>
                
                {% for linha in ['21', '22', '23', '24', '31', '32', '33'] %}
                    <div class="linha-card" style="border-left-color: #8e44ad;">
                        <h4 style="color: #8e44ad; background: rgba(142, 68, 173, 0.1);">{{ t('Linha') }} {{ linha }}</h4>
                        <div class="machine-section">
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem PKIRIS (Lateral):') }}</label>
                                <input type="text" name="pkiris_src_{{ linha }}_lateral" id="pkiris_src_{{ linha }}_lateral" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('pkiris_src_{{ linha }}_lateral')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem PKIRIS (Fundo):') }}</label>
                                <input type="text" name="pkiris_src_{{ linha }}_fundo" id="pkiris_src_{{ linha }}_fundo" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('pkiris_src_{{ linha }}_fundo')"><i class="fas fa-folder-open"></i></button>
                            </div>
                        </div>
                    </div>
                {% endfor %}
                
                <div class="linha-card" style="border-left-color: #8e44ad;">
                    <h4 style="color: #8e44ad; background: rgba(142, 68, 173, 0.1);">{{ t('Linha 34') }}</h4>
                    <div class="ramal-container">
                        {% for ramal in [1, 2] %}
                        <div class="ramal">
                            <h5>{{ t('Ramal') }} {{ ramal }}</h5>
                            {% for maq in ['lateral', 'fundo'] %}
                            {% set maquina = maq ~ ramal %}
                            <div class="path-input-group">
                                <label style="min-width: 150px;">{{ t('Origem ') }}{{ t(maq.capitalize()) }} {{ ramal }}:</label>
                                <input type="text" name="pkiris_src_34_{{ maquina }}" id="pkiris_src_34_{{ maquina }}" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('pkiris_src_34_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            {% endfor %}
                        </div>
                        {% endfor %}
                    </div>
                </div>

                <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> {{ t('Gravar Configurações PKIRIS') }}</button>
            </form>
        </div>
        
        <div class="section" style="border-left-color: #27ae60;">
            <h3><i class="fas fa-play-circle"></i> {{ t('Controlo do Serviço PKIRIS') }}</h3>
            <p>{{ t('O serviço corre automaticamente em background para verificar novos ficheiros.') }}</p>
            <button type="button" class="btn btn-primary" onclick="startPkirisBackup()"><i class="fas fa-play"></i> {{ t('Iniciar Backup PKIRIS') }}</button>
            <button type="button" class="btn btn-danger" onclick="stopPkirisBackup()"><i class="fas fa-stop"></i> {{ t('Parar Backup PKIRIS') }}</button>
            <span class="service-led" id="service_pkiris" title="{{ t('Status do serviço PKIRIS') }}"></span>
        </div>
    </div>
    
    <div id="backup_historicos" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-history"></i> {{ t('Configuração Global Backup Históricos') }}</h3>
            <p>{{ t('Define a pasta de destino raiz. A árvore criada será do tipo: /Linha_XX/Lateral/Mês/Dia/. Executa periodicamente (aos 55m).') }}</p>
            <form id="historicosConfigForm">
                <div class="path-input-group">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Retenção Históricos (dias):') }}</label>
                    <input type="number" name="historicos_retention_days" id="historicos_retention_days" class="form-control" style="max-width: 150px;" placeholder="365">
                </div>
                <div class="path-input-group" style="margin-bottom: 20px;">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Pasta Destino Raiz Históricos:') }}</label>
                    <input type="text" name="historicos_dst_root" id="historicos_dst_root" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('historicos_dst_root')"><i class="fas fa-folder-open"></i></button>
                </div>
                
                <h4 style="margin-top:20px; border-bottom:2px solid #ccc; padding-bottom:5px; color: #2c3e50;"><i class="fas fa-sitemap"></i> {{ t('Diretorias de Origem (Pastas dos Históricos)') }}</h4>
                
                {% for linha in ['21', '22', '23', '24', '31', '32', '33'] %}
                    <div class="linha-card" style="border-left-color: #34495e;">
                        <h4 style="color: #34495e; background: rgba(52, 73, 94, 0.1);">{{ t('Linha') }} {{ linha }}</h4>
                        <div class="machine-section">
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem Histórico (Lateral):') }}</label>
                                <input type="text" name="historico_src_{{ linha }}_lateral" id="historico_src_{{ linha }}_lateral" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('historico_src_{{ linha }}_lateral')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem Histórico (Fundo):') }}</label>
                                <input type="text" name="historico_src_{{ linha }}_fundo" id="historico_src_{{ linha }}_fundo" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('historico_src_{{ linha }}_fundo')"><i class="fas fa-folder-open"></i></button>
                            </div>
                        </div>
                    </div>
                {% endfor %}
                
                <div class="linha-card" style="border-left-color: #34495e;">
                    <h4 style="color: #34495e; background: rgba(52, 73, 94, 0.1);">{{ t('Linha 34') }}</h4>
                    <div class="ramal-container">
                        {% for ramal in [1, 2] %}
                        <div class="ramal">
                            <h5>{{ t('Ramal') }} {{ ramal }}</h5>
                            {% for maq in ['lateral', 'fundo'] %}
                            {% set maquina = maq ~ ramal %}
                            <div class="path-input-group">
                                <label style="min-width: 150px;">{{ t('Origem ') }}{{ t(maq.capitalize()) }} {{ ramal }}:</label>
                                <input type="text" name="historico_src_34_{{ maquina }}" id="historico_src_34_{{ maquina }}" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('historico_src_34_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            {% endfor %}
                        </div>
                        {% endfor %}
                    </div>
                </div>

                <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> {{ t('Gravar Configurações Históricos') }}</button>
            </form>
        </div>
        
        <div class="section" style="border-left-color: #27ae60;">
            <h3><i class="fas fa-play-circle"></i> {{ t('Controlo do Serviço Históricos') }}</h3>
            <button type="button" class="btn btn-primary" onclick="startHistoricosBackup()"><i class="fas fa-play"></i> {{ t('Iniciar Backup Históricos') }}</button>
            <button type="button" class="btn btn-danger" onclick="stopHistoricosBackup()"><i class="fas fa-stop"></i> {{ t('Parar Backup Históricos') }}</button>
            <span class="service-led" id="service_historicos" title="{{ t('Status do serviço Históricos') }}"></span>
        </div>
    </div>
    
    <div id="backup_artigos" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-box-open"></i> {{ t('Configuração Global Backup Artigos') }}</h3>
            <p>{{ t('Define a pasta de destino raiz. A árvore criada será do tipo: /Linha_XX/Lateral/Mês/Dia/. Executa periodicamente.') }}</p>
            <form id="artigosConfigForm">
                <div class="path-input-group">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Retenção Artigos (dias):') }}</label>
                    <input type="number" name="artigos_retention_days" id="artigos_retention_days" class="form-control" style="max-width: 150px;" placeholder="365">
                </div>
                <div class="path-input-group" style="margin-bottom: 20px;">
                    <label style="min-width: 200px; font-weight: bold;">{{ t('Pasta Destino Raiz Artigos:') }}</label>
                    <input type="text" name="artigos_dst_root" id="artigos_dst_root" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('artigos_dst_root')"><i class="fas fa-folder-open"></i></button>
                </div>
                
                <h4 style="margin-top:20px; border-bottom:2px solid #ccc; padding-bottom:5px; color: #2c3e50;"><i class="fas fa-sitemap"></i> {{ t('Diretorias de Origem (Pastas dos Artigos)') }}</h4>
                
                {% for linha in ['21', '22', '23', '24', '31', '32', '33'] %}
                    <div class="linha-card" style="border-left-color: #d35400;">
                        <h4 style="color: #d35400; background: rgba(211, 84, 0, 0.1);">{{ t('Linha') }} {{ linha }}</h4>
                        <div class="machine-section">
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem Artigo (Lateral):') }}</label>
                                <input type="text" name="artigo_src_{{ linha }}_lateral" id="artigo_src_{{ linha }}_lateral" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('artigo_src_{{ linha }}_lateral')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            <div class="path-input-group">
                                <label style="min-width: 200px;">{{ t('Origem Artigo (Fundo):') }}</label>
                                <input type="text" name="artigo_src_{{ linha }}_fundo" id="artigo_src_{{ linha }}_fundo" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('artigo_src_{{ linha }}_fundo')"><i class="fas fa-folder-open"></i></button>
                            </div>
                        </div>
                    </div>
                {% endfor %}
                
                <div class="linha-card" style="border-left-color: #d35400;">
                    <h4 style="color: #d35400; background: rgba(211, 84, 0, 0.1);">{{ t('Linha 34') }}</h4>
                    <div class="ramal-container">
                        {% for ramal in [1, 2] %}
                        <div class="ramal">
                            <h5>{{ t('Ramal') }} {{ ramal }}</h5>
                            {% for maq in ['lateral', 'fundo'] %}
                            {% set maquina = maq ~ ramal %}
                            <div class="path-input-group">
                                <label style="min-width: 150px;">{{ t('Origem ') }}{{ t(maq.capitalize()) }} {{ ramal }}:</label>
                                <input type="text" name="artigo_src_34_{{ maquina }}" id="artigo_src_34_{{ maquina }}" class="form-control">
                                <button type="button" class="browse-btn" onclick="openFileBrowser('artigo_src_34_{{ maquina }}')"><i class="fas fa-folder-open"></i></button>
                            </div>
                            {% endfor %}
                        </div>
                        {% endfor %}
                    </div>
                </div>

                <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> {{ t('Gravar Configurações Artigos') }}</button>
            </form>
        </div>
        
        <div class="section" style="border-left-color: #27ae60;">
            <h3><i class="fas fa-play-circle"></i> {{ t('Controlo do Serviço Artigos') }}</h3>
            <button type="button" class="btn btn-primary" onclick="startArtigosBackup()"><i class="fas fa-play"></i> {{ t('Iniciar Backup Artigos') }}</button>
            <button type="button" class="btn btn-danger" onclick="stopArtigosBackup()"><i class="fas fa-stop"></i> {{ t('Parar Backup Artigos') }}</button>
            <span class="service-led" id="service_artigos" title="{{ t('Status do serviço Artigos') }}"></span>
        </div>
    </div>

    <div id="servicos" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-server"></i> {{ t('Status dos Serviços e Contadores') }}</h3>
            <table class="service-table">
                <thead><tr><th>{{ t('Linha') }}</th><th>{{ t('Máquina') }}</th><th>{{ t('Backup Ativo') }}</th><th>{{ t('Mosaico Ativo') }}</th><th>{{ t('Contador Turno (JPG/XML)') }}</th><th>{{ t('Contador Diário (JPG/XML)') }}</th></tr></thead>
                <tbody id="service-status-table-body"></tbody>
            </table>
        </div>
        <div class="section">
            <h3><i class="fas fa-power-off"></i> {{ t('Controlo Global') }}</h3>
            <div style="text-align: center; margin-bottom: 2rem;">
                <button class="btn btn-success" onclick="startAll()"><i class="fas fa-play"></i> {{ t('INICIAR TUDO') }}</button>
                <button class="btn btn-danger" onclick="stopAll()"><i class="fas fa-stop"></i> {{ t('PARAR TUDO') }}</button>
                <button type="button" class="btn btn-info" onclick="checkServices()"><i class="fas fa-check"></i> {{ t('Check Serviços') }}</button>
            </div>
        </div>
    </div>

    <div id="exportar" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-download"></i> {{ t('Exportação Seletiva') }}</h3>
            <form id="exportForm">
                <div class="export-grid">
                    <div class="export-section">
                        <h4><i class="fas fa-calendar-alt"></i> {{ t('Data de Exportação') }}</h4>
                        <div class="form-group"><input type="date" name="export_date" class="form-control" required></div>
                    </div>
                    <div class="export-section">
                        <h4><i class="fas fa-clock"></i> {{ t('Turnos') }}</h4>
                        <div class="checkbox-list">
                            <div class="checkbox-item"><input type="checkbox" name="turnos" value="1" id="export_turno1"><label for="export_turno1">{{ t('1º Turno (06:00-14:00)') }}</label></div>
                            <div class="checkbox-item"><input type="checkbox" name="turnos" value="2" id="export_turno2"><label for="export_turno2">{{ t('2º Turno (14:00-22:00)') }}</label></div>
                            <div class="checkbox-item"><input type="checkbox" name="turnos" value="3" id="export_turno3"><label for="export_turno3">{{ t('3º Turno (22:00-06:00)') }}</label></div>
                        </div>
                    </div>
                    <div class="export-section">
                        <h4><i class="fas fa-industry"></i> {{ t('Máquinas de Inspeção') }}</h4>
                        <div class="checkbox-list" id="machines-list"></div>
                    </div>
                </div>
                <div class="form-group"><label><input type="checkbox" name="compress" checked> {{ t('Comprimir exportação (ZIP)') }}</label></div>
                <button type="submit" class="btn btn-primary" id="btn-export-submit"><i class="fas fa-download"></i> {{ t('Iniciar Exportação') }}</button>
            </form>
            <div id="exportProgress" style="display: none; margin-top: 20px;">
                <p id="progressText" style="font-weight: bold; margin-bottom: 5px;">{{ t('A preparar ficheiros... 0%') }}</p>
                <div class="progress"><div class="progress-bar" id="progressBar" style="width: 0%">0%</div></div>
            </div>
        </div>
    </div>

    <div id="diagnostics" class="tab-content">
        <div class="stats-grid">
            <div class="stats-card"><span class="stats-value" id="current-shift">-</span><div class="stats-label">{{ t('Turno Atual') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="volume1-usage">-</span><div class="stats-label">{{ t('Volume1 Usado') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="volume2-usage">-</span><div class="stats-label">{{ t('Volume2 (SSD) Usado') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="system-load">-</span><div class="stats-label">{{ t('CPU Usage') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="memory-usage">-</span><div class="stats-label">{{ t('RAM Usage') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="files-copied-shift">0</span><div class="stats-label">{{ t('Arquivos Copiados (Turno)') }}</div></div>
            <div class="stats-card"><span class="stats-value" id="files-copied-day">0</span><div class="stats-label">{{ t('Arquivos Copiados (Dia)') }}</div></div>
        </div>
        <div class="section">
            <h3><i class="fas fa-heartbeat"></i> {{ t('Estado dos Serviços') }}</h3>
            <div id="services-status"><p>{{ t('Carregando estado dos serviços...') }}</p></div>
            <button class="btn btn-info" onclick="checkServices()"><i class="fas fa-check"></i> {{ t('Check Serviços') }}</button>
        </div>
        <div id="directories-list-container" class="section">
            <h3><i class="fas fa-folder"></i> {{ t('Diretórios Gravados') }}</h3>
            <div id="directories-list" style="max-height:200px; overflow-y:auto; background:#f8f9fa; padding:1rem; font-family:monospace;"></div>
            <button class="btn btn-info" onclick="loadDirectories()"><i class="fas fa-folder-open"></i> {{ t('Check Diretórios') }}</button>
        </div>
        <div class="section">
            <h3><i class="fas fa-network-wired"></i> {{ t('IPs Conectados') }}</h3>
            <div class="ip-list" id="connected-ips"><p>{{ t('Carregando lista de IPs...') }}</p></div>
            <button class="btn btn-info" onclick="loadConnectedIPs()"><i class="fas fa-sync"></i> {{ t('Atualizar IPs') }}</button>
        </div>
        <div class="section">
            <h3><i class="fas fa-copy"></i> {{ t('Status da Cópia de Arquivos') }}</h3>
            <div id="copy-status"><p>{{ t('Sistema de cópia parado') }}</p></div>
        </div>
    </div>
    
    <div id="gestao_mosaico" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-tasks"></i> {{ t('Gestão de Processos do Mosaico') }}</h3>
            <button class="btn btn-info" onclick="loadMosaicStatus()"><i class="fas fa-sync"></i> {{ t('Atualizar Status') }}</button>
            <table class="service-table">
                <thead><tr><th>{{ t('Linha') }}</th><th>{{ t('Máquina') }}</th><th>{{ t('Config') }}</th><th>{{ t('Status') }}</th><th>{{ t('PID') }}</th><th>{{ t('Porta') }}</th><th>{{ t('URL') }}</th><th>{{ t('Ações') }}</th></tr></thead>
                <tbody id="mosaic-status-table-body"></tbody>
            </table>
        </div>
    </div>

    <div id="config_mosaico" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-th"></i> {{ t('Configurações do Mosaico') }}</h3>
            <div class="form-group">
                <label for="mosaic_line_selector">{{ t('Linha de Produção / Visão Global:') }}</label>
                <select id="mosaic_line_selector" class="form-control" onchange="loadMosaicConfig()">
                    <option value="global">{{ t('Configuração Global (Padrão)') }}</option>
                    <option value="overview_lateral">{{ t('Visão Global - Laterais') }}</option>
                    <option value="overview_fundo">{{ t('Visão Global - Fundos') }}</option>
                    <option value="21">{{ t('Linha 21') }}</option>
                    <option value="22">{{ t('Linha 22') }}</option>
                    <option value="23">{{ t('Linha 23') }}</option>
                    <option value="24">{{ t('Linha 24') }}</option>
                    <option value="31">{{ t('Linha 31') }}</option>
                    <option value="32">{{ t('Linha 32') }}</option>
                    <option value="33">{{ t('Linha 33') }}</option>
                    <option value="34">{{ t('Linha 34') }}</option>
                </select>
            </div>

            <div id="overview_mosaic_config" style="display:none; padding: 15px; background: #fffcf5; border-left: 4px solid #f39c12; margin-bottom: 20px;">
                <h4 style="color: #f39c12; border-bottom: 2px solid #fce3b6; padding-bottom: 5px;"><i class="fas fa-tv"></i> {{ t('Definições de Ecrã 4K') }}</h4>
                <div class="form-group">
                    <label>{{ t('Orientação do Ecrã (Rotação):') }}</label>
                    <select id="overview_orientation" class="form-control">
                        <option value="0">{{ t('Normal (0º)') }}</option>
                        <option value="90">{{ t('Vertical para a Direita (90º)') }}</option>
                        <option value="180">{{ t('Invertido (180º)') }}</option>
                        <option value="270">{{ t('Vertical para a Esquerda (270º)') }}</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>{{ t('Layout das Linhas:') }}</label>
                    <select id="overview_layout" class="form-control">
                        <option value="horizontal">{{ t('Horizontal (Barras empilhadas)') }}</option>
                        <option value="vertical">{{ t('Vertical (Colunas lado a lado)') }}</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>{{ t('Máximo de Imagens por Máquina:') }}</label>
                    <input type="number" id="overview_max_images" class="form-control" placeholder="Ex: 8">
                </div>
                <h4 style="color: #f39c12; margin-top: 15px;"><i class="fas fa-check-square"></i> {{ t('Máquinas a Exibir neste Ecrã') }}</h4>
                <div id="overview_machines_list" class="checkbox-list" style="max-height: 250px; overflow-y: auto;"></div>
                <button type="button" class="btn btn-warning" onclick="saveOverviewConfig()" style="margin-top: 15px;"><i class="fas fa-save"></i> {{ t('Gravar Visão Global') }}</button>
            </div>

            <form id="normal_mosaic_config">
                <div class="path-input-group">
                    <label>{{ t('Pasta de origem para análise (XML):') }}</label>
                    <input type="text" name="mosaic_source_path" id="mosaic_source_path" class="form-control">
                    <button type="button" class="browse-btn" onclick="openFileBrowser('mosaic_source_path')"><i class="fas fa-folder-open"></i> {{ t('Procurar') }}</button>
                </div>
                <h4><i class="fas fa-file-code"></i> {{ t('Campos de Dados XML') }}</h4>
                <button type="button" class="btn btn-primary" id="analyzeXmlBtn" onclick="analyzeXmlFields()"><i class="fas fa-search"></i> {{ t('Re-Analisar Campos XML') }}</button>
                <div id="xmlFieldsCheckboxes" class="checkbox-list"><p>{{ t('Carregando campos salvos ou pressione "Analisar" para procurar novos.') }}</p></div>
                <h4><i class="fas fa-images"></i> {{ t('Sobreposição de Texto na Imagem') }}</h4>
                <div class="form-group"><label for="overlay_top">{{ t('Texto Superior:') }}</label><select id="overlay_top" class="form-control"></select></div>
                <div class="form-group"><label for="overlay_bottom_left">{{ t('Inferior Esquerdo:') }}</label><select id="overlay_bottom_left" class="form-control"></select></div>
                <div class="form-group"><label for="overlay_bottom_right">{{ t('Inferior Direito:') }}</label><select id="overlay_bottom_right" class="form-control"></select></div>
                <h4><i class="fas fa-filter"></i> {{ t('Filtros de Câmera') }}</h4>
                <button type="button" class="btn btn-primary" id="analyzeNumCamBtn" onclick="analyzeNumCamValues()"><i class="fas fa-camera"></i> {{ t('Analisar Câmeras (NUM_CAM)') }}</button>
                <div id="numCamFilterCheckboxes" class="checkbox-list"><p>{{ t('Pressione o botão acima para listar os filtros de câmera disponíveis.') }}</p></div>
                <h4><i class="fas fa-border-all"></i> {{ t('Configuração de Exibição') }}</h4>
                <div class="form-group">
                    <label>{{ t('Orientação do Ecrã Mosaico (Graus):') }}</label>
                    <select id="normal_orientation" class="form-control">
                        <option value="0">{{ t('Normal (0º)') }}</option>
                        <option value="90">{{ t('Vertical Dir (90º)') }}</option>
                        <option value="180">{{ t('Invertido (180º)') }}</option>
                        <option value="270">{{ t('Vertical Esq (270º)') }}</option>
                    </select>
                </div>
                <div class="form-group"><label>{{ t('Colunas da grade:') }}</label><input type="number" id="grid_columns" class="form-control"></div>
                <div class="form-group"><label>{{ t('Linhas da grade:') }}</label><input type="number" id="grid_lines" class="form-control"></div>
                <div class="form-group"><label>{{ t('Tamanho da imagem (px):') }}</label><input type="number" id="image_size" class="form-control"></div>
                <div class="form-group"><label>{{ t('Máximo de imagens armazenadas:') }}</label><input type="number" id="max_images" class="form-control"></div>
                <div class="form-group"><label>{{ t('Intervalo de atualização (seg):') }}</label><input type="number" id="refresh_interval" class="form-control"></div>
                <div class="form-group"><label>{{ t('Percentagem de Zoom (duplo clique):') }}</label><input type="number" id="zoom_percentage" class="form-control"></div>
                <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> {{ t('Gravar Configuração do Mosaico') }}</button>
            </form>
        </div>
    </div>
    
    <div id="users" class="tab-content">
        <div class="grid-2-col">
            <div class="section">
                <h3><i class="fas fa-user-plus"></i> {{ t('Criar Novo Utilizador') }}</h3>
                <form id="createUserForm">
                    <div class="form-group"><label>{{ t('Nome de Utilizador') }}</label><input type="text" id="new_username" name="new_username" class="form-control" required></div>
                    <div class="form-group"><label>{{ t('Password') }}</label><input type="password" id="new_password" name="new_password" class="form-control" required></div>
                    <div class="form-group"><label>{{ t('Confirmar Password') }}</label><input type="password" id="confirm_password" name="confirm_password" class="form-control" required></div>
                    <button type="submit" class="btn btn-primary"><i class="fas fa-plus"></i> {{ t('Criar Utilizador') }}</button>
                </form>
            </div>
            <div class="section">
                <h3><i class="fas fa-key"></i> {{ t('Alterar a Minha Password') }}</h3>
                <form id="changePasswordForm">
                    {% if not session.is_dev %}
                    <div class="form-group"><label>{{ t('Password Atual') }}</label><input type="password" id="current_password" name="current_password" class="form-control" required></div>
                    {% endif %}
                    <div class="form-group"><label>{{ t('Nova Password') }}</label><input type="password" id="change_new_password" name="new_password" class="form-control" required></div>
                    <div class="form-group"><label>{{ t('Confirmar Nova Password') }}</label><input type="password" id="change_confirm_password" name="confirm_password" class="form-control" required></div>
                    <button type="submit" class="btn btn-warning"><i class="fas fa-save"></i> {{ t('Alterar Password') }}</button>
                </form>
            </div>
        </div>
        <div class="section">
            <h3><i class="fas fa-users-cog"></i> {{ t('Gerir Utilizadores') }}</h3>
            <table class="user-table">
                <thead><tr><th>{{ t('Utilizador') }}</th><th>{{ t('Tipo') }}</th><th>{{ t('Ações') }}</th></tr></thead>
                <tbody id="user-list-body"></tbody>
            </table>
        </div>
    </div>

    <div id="logs" class="tab-content">
        <div class="section">
            <h3><i class="fas fa-file-alt"></i> {{ t('Logs do Sistema') }}</h3>
            <div style="margin-bottom: 1rem;">
                <button class="btn btn-info" onclick="refreshLogs()"><i class="fas fa-sync"></i> {{ t('Atualizar') }}</button>
                <button class="btn btn-warning" onclick="clearLogs()"><i class="fas fa-trash"></i> {{ t('Limpar Logs') }}</button>
                <button class="btn btn-primary" onclick="downloadLogs()"><i class="fas fa-download"></i> {{ t('Download') }}</button>
            </div>
            <div id="logs-content" style="background: #1e1e1e; color: #fff; padding: 1rem; border-radius: 6px; font-family: monospace; height: 400px; overflow-y: auto;">{{ t('Carregando logs...') }}</div>
        </div>
    </div>
</div>

<div id="fileBrowserModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h3><i class="fas fa-folder-open"></i> {{ t('Navegador de Arquivos') }}</h3>
            <button class="close" onclick="closeFileBrowser()">&times;</button>
        </div>
        <div class="modal-body">
            <div class="current-path" id="currentPath">/</div>
            <ul class="file-list" id="fileList"><li>{{ t('Carregando...') }}</li></ul>
        </div>
        <div class="modal-footer">
            <button class="btn btn-primary" onclick="selectCurrentPath()"><i class="fas fa-check"></i> {{ t('Selecionar') }}</button>
            <button class="btn btn-secondary" onclick="closeFileBrowser()"><i class="fas fa-times"></i> {{ t('Cancelar') }}</button>
        </div>
    </div>
</div>

<script>
    const IS_DEV = {{ session.is_dev | tojson }};
    let currentInputId = '';
    let currentPath = '/';
    let mosaicConfig = {};
    let aaInterval = null;

    function showTab(tabName) {
        document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        const tabContent = document.getElementById(tabName);
        if (tabContent) tabContent.classList.add('active');
        const clickedTab = document.querySelector(`.tab[onclick="showTab('${tabName}')"]`);
        if (clickedTab) clickedTab.classList.add('active');

        if (tabName === 'configuracao') { setTimeout(checkAllPaths, 500); setTimeout(updateAllServiceStatus, 1000); pollArticleAnalysis(); }
        else if (tabName === 'servicos') { loadServiceStatusAndCounters(); }
        else if (tabName === 'diagnostics') { loadDiagnostics(); }
        else if (tabName === 'logs') { refreshLogs(); }
        else if (tabName === 'exportar') { loadMachinesList(); }
        else if (tabName === 'config_mosaico') { loadMosaicConfig(); }
        else if (tabName === 'gestao_mosaico') { loadMosaicStatus(); }
        else if (tabName === 'users') { loadUserList(); }
    }

    function loadConfigurationsFromServer() {
        fetch('/get_config').then(response => response.json()).then(config => { populateAllFormFields(config); }).catch(error => { showAlert('{{ t("Erro ao carregar configurações.") }}', 'danger'); });
    }

    function populateAllFormFields(config) {
        try {
            const generalFields = ['ssd_path', 'mirror_source_path', 'mosaic_source_path', 'mosaic_config_folder', 'log_file_path', 'ssd_retention_days', 'hdd_retention_months', 'scan_interval_sec', 'article_analysis_path'];
            generalFields.forEach(fieldName => {
                const field = document.getElementById(fieldName) || document.querySelector(`[name="${fieldName}"]`);
                if (field && config[fieldName] !== undefined) field.value = config[fieldName];
            });

            const mirrorCheckbox = document.getElementById('mirror_include_subfolders');
            if (mirrorCheckbox) mirrorCheckbox.checked = config.mirror_include_subfolders !== false;

            if (config.pkiris_retention_days !== undefined) document.getElementById('pkiris_retention_days').value = config.pkiris_retention_days;
            if (config.pkiris_dst_root !== undefined) document.getElementById('pkiris_dst_root').value = config.pkiris_dst_root;

            if (config.historicos_retention_days !== undefined) document.getElementById('historicos_retention_days').value = config.historicos_retention_days;
            if (config.historicos_dst_root !== undefined) document.getElementById('historicos_dst_root').value = config.historicos_dst_root;

            if (config.artigos_retention_days !== undefined) document.getElementById('artigos_retention_days').value = config.artigos_retention_days;
            if (config.artigos_dst_root !== undefined) document.getElementById('artigos_dst_root').value = config.artigos_dst_root;

            if (config.turnos) {
                Object.keys(config.turnos).forEach(turno => {
                    const inicioField = document.querySelector(`[name="${turno}_inicio"]`);
                    const fimField = document.querySelector(`[name="${turno}_fim"]`);
                    if (inicioField && config.turnos[turno].inicio) inicioField.value = config.turnos[turno].inicio;
                    if (fimField && config.turnos[turno].fim) fimField.value = config.turnos[turno].fim;
                });
            }

            if (config.visao_global) {
                const vgPortLat = document.getElementById('overview_port_lateral');
                const vgPortFun = document.getElementById('overview_port_fundo');
                const vgCycle = document.getElementById('overview_cycle_mode_active');
                const vgTime = document.getElementById('overview_cycle_time_sec');
                if (vgPortLat) vgPortLat.value = config.visao_global.port_lateral || 5098;
                if (vgPortFun) vgPortFun.value = config.visao_global.port_fundo || 5099;
                if (vgCycle) vgCycle.checked = config.visao_global.cycle_mode_active;
                if (vgTime) vgTime.value = config.visao_global.cycle_time_sec;
            }

            if (config.linhas) {
                Object.keys(config.linhas).forEach(linha => {
                    const linhaConfig = config.linhas[linha];
                    
                    const cycleModeInput = document.getElementById(`cycle_mode_active_${linha}`);
                    if(cycleModeInput && linhaConfig.cycle_mode_active !== undefined) cycleModeInput.checked = linhaConfig.cycle_mode_active;
                    
                    const cycleTimeInput = document.getElementById(`cycle_time_sec_${linha}`);
                    if(cycleTimeInput && linhaConfig.cycle_time_sec !== undefined) cycleTimeInput.value = linhaConfig.cycle_time_sec;
                    
                    const testModeInput = document.getElementById(`use_test_mode_${linha}`);
                    if(testModeInput && linhaConfig.use_test_mode !== undefined) testModeInput.checked = linhaConfig.use_test_mode;

                    Object.keys(linhaConfig).forEach(maquina => {
                        if (typeof linhaConfig[maquina] !== 'object' || linhaConfig[maquina] === null) return;
                        const maquinaConfig = linhaConfig[maquina];
                        
                        const origemProdField = document.getElementById(`origem_prod_${linha}_${maquina}`);
                        const destinoProdField = document.getElementById(`destino_prod_${linha}_${maquina}`);
                        const origemTestField = document.getElementById(`origem_test_${linha}_${maquina}`);
                        const destinoTestField = document.getElementById(`destino_test_${linha}_${maquina}`);
                        
                        const portInput = document.getElementById(`mosaic_port_${linha}_${maquina}`);
                        const deleteSourceField = document.getElementById(`delete_source_${linha}_${maquina}`);
                        
                        const pkirisSrcField = document.getElementById(`pkiris_src_${linha}_${maquina}`);
                        const historicoSrcField = document.getElementById(`historico_src_${linha}_${maquina}`);
                        const artigoSrcField = document.getElementById(`artigo_src_${linha}_${maquina}`);

                        if (origemProdField) origemProdField.value = maquinaConfig.src_prod || maquinaConfig.src || '';
                        if (destinoProdField) destinoProdField.value = maquinaConfig.dst_prod || maquinaConfig.dst || '';
                        if (origemTestField) origemTestField.value = maquinaConfig.src_test || '';
                        if (destinoTestField) destinoTestField.value = maquinaConfig.dst_test || '';
                        
                        if (deleteSourceField) deleteSourceField.checked = maquinaConfig.delete_source !== false;
                        if (portInput && maquinaConfig.mosaic_port) portInput.value = maquinaConfig.mosaic_port;
                        
                        if (pkirisSrcField && maquinaConfig.pkiris_src !== undefined) pkirisSrcField.value = maquinaConfig.pkiris_src;
                        if (historicoSrcField && maquinaConfig.historico_src !== undefined) historicoSrcField.value = maquinaConfig.historico_src;
                        if (artigoSrcField && maquinaConfig.artigo_src !== undefined) artigoSrcField.value = maquinaConfig.artigo_src;
                    });
                });
            }
        } catch (error) {}
    }
    
    function loadMosaicConfig() {
        const linhaSelect = document.getElementById('mosaic_line_selector');
        const linha = linhaSelect ? linhaSelect.value : 'global';
        fetch('/api/mosaic_config?linha=' + linha).then(response => response.json()).then(config => {
            mosaicConfig = config;
            if (linha === 'overview_lateral' || linha === 'overview_fundo') {
                document.getElementById('normal_mosaic_config').style.display = 'none';
                document.getElementById('overview_mosaic_config').style.display = 'block';
                document.getElementById('overview_orientation').value = mosaicConfig.orientation || 0;
                document.getElementById('overview_layout').value = mosaicConfig.layout || 'horizontal';
                document.getElementById('overview_max_images').value = mosaicConfig.max_images || 8;
                renderOverviewMachines(mosaicConfig.active_machines || {}, mosaicConfig.all_available_machines || {});
            } else {
                document.getElementById('normal_mosaic_config').style.display = 'block';
                document.getElementById('overview_mosaic_config').style.display = 'none';
                document.getElementById('normal_orientation').value = mosaicConfig.display_config?.orientation || 0;
                document.getElementById('grid_columns').value = mosaicConfig.display_config?.grid_columns || '';
                document.getElementById('grid_lines').value = mosaicConfig.display_config?.grid_lines || '';
                document.getElementById('image_size').value = mosaicConfig.display_config?.image_size || '';
                document.getElementById('max_images').value = mosaicConfig.display_config?.max_images || '';
                document.getElementById('refresh_interval').value = mosaicConfig.display_config?.refresh_interval || '';
                document.getElementById('zoom_percentage').value = mosaicConfig.display_config?.zoom_percentage || 250;

                const availableFields = mosaicConfig.xml_config?.available_xml_fields || [];
                if (availableFields.length > 0) renderXmlFields(availableFields);
                else document.getElementById('xmlFieldsCheckboxes').innerHTML = `<p>{{ t('Pressione "Analisar Campos XML" para listar os campos disponíveis.') }}</p>`;

                const filterConfig = mosaicConfig.filter_config?.NUM_CAM || {};
                const numCamContainer = document.getElementById('numCamFilterCheckboxes');
                numCamContainer.innerHTML = '';
                if (filterConfig.available_values && filterConfig.available_values.length > 0) {
                    filterConfig.available_values.forEach(val => {
                        const isChecked = filterConfig.selected_values?.includes(val) ? 'checked' : '';
                        numCamContainer.innerHTML += `<div class="checkbox-item"><input type="checkbox" name="num_cam_filter" value="${val}" id="filter_cam_${val}" ${isChecked}><label for="filter_cam_${val}">{{ t('Câmara') }} ${val}</label></div>`;
                    });
                } else {
                    numCamContainer.innerHTML = `<p>{{ t('Nenhum filtro de câmera salvo. Pressione o botão para analisar.') }}</p>`;
                }
            }
        }).catch(error => { showAlert('{{ t("Erro ao carregar configurações do mosaico.") }}', 'danger'); });
    }

    function renderOverviewMachines(activeMachines, allMachines) {
        const container = document.getElementById('overview_machines_list');
        container.innerHTML = '';
        for (const [l, maquinas] of Object.entries(allMachines)) {
            maquinas.forEach(maq => {
                const isChecked = (activeMachines[l] && activeMachines[l].includes(maq)) ? 'checked' : '';
                container.innerHTML += `<div class="checkbox-item" style="width:200px; display:inline-block; margin-right: 10px;"><input type="checkbox" name="ov_machine" value="${l}|${maq}" id="ov_${l}_${maq}" ${isChecked}><label for="ov_${l}_${maq}">{{ t('Linha') }} ${l} - ${maq}</label></div>`;
            });
        }
    }

    function saveOverviewConfig() {
        const linhaSelect = document.getElementById('mosaic_line_selector').value; 
        const orientation = parseInt(document.getElementById('overview_orientation').value) || 0;
        const layout = document.getElementById('overview_layout').value || 'horizontal';
        const maxImages = parseInt(document.getElementById('overview_max_images').value) || 8;
        const activeMachines = {};
        document.querySelectorAll('input[name="ov_machine"]:checked').forEach(cb => {
            const parts = cb.value.split('|');
            if (!activeMachines[parts[0]]) activeMachines[parts[0]] = [];
            activeMachines[parts[0]].push(parts[1]);
        });
        fetch('/save_mosaic_config?linha=' + linhaSelect, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ orientation: orientation, layout: layout, max_images: maxImages, active_machines: activeMachines })
        }).then(r => r.json()).then(r => showAlert(r.message, r.status === 'success' ? 'success' : 'danger'));
    }

    function renderXmlFields(fields) {
        const container = document.getElementById('xmlFieldsCheckboxes');
        container.innerHTML = '';
        if (fields.length === 0) {
            container.innerHTML = `<p style="color: #e74c3c;"><b>Indisponível:</b> Nenhum arquivo XML encontrado ou nenhum campo salvo.</p>`;
        } else {
             container.innerHTML = `<div class="checkbox-item-alias" style="font-weight: bold;"><span></span><label>Nome do Campo (Original)</label><label>Nome para Exibição (Alias)</label></div>`;
            fields.forEach(field => {
                const savedAlias = mosaicConfig.xml_config?.selectedFields?.[field] || '';
                const isChecked = mosaicConfig.xml_config?.selectedFields?.hasOwnProperty(field) ? 'checked' : '';
                container.innerHTML += `<div class="checkbox-item-alias"><input type="checkbox" name="xml_fields" value="${field}" id="field_${field}" ${isChecked} onchange="updateOverlayDropdowns()"><label for="field_${field}">${field}</label><input type="text" class="form-control" name="alias_${field}" value="${savedAlias}" placeholder="(padrão: ${field})"></div>`;
            });
        }
        updateOverlayDropdowns();
    }

    function analyzeXmlFields() {
        const analyzeBtn = document.getElementById('analyzeXmlBtn');
        analyzeBtn.innerHTML = '<i class="fas fa-sync fa-spin"></i> {{ t("Analisando...") }}';
        analyzeBtn.disabled = true;
        const pathElem = document.getElementById('mosaic_source_path');
        const path = pathElem ? pathElem.value : '';
        fetch('/analyze_xml_fields', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: path })
        }).then(response => response.json()).then(fields => {
            if (!mosaicConfig.xml_config) mosaicConfig.xml_config = {};
            mosaicConfig.xml_config.available_xml_fields = fields;
            renderXmlFields(fields);
            analyzeBtn.innerHTML = '<i class="fas fa-search"></i> {{ t("Re-Analisar Campos XML") }}';
            analyzeBtn.disabled = false;
            showAlert('{{ t("Análise de campos XML concluída!") }}', 'success');
        }).catch(error => {
            document.getElementById('xmlFieldsCheckboxes').innerHTML = `<p style="color:red;">{{ t("Erro ao analisar.") }}</p>`;
            analyzeBtn.innerHTML = '<i class="fas fa-search"></i> {{ t("Re-Analisar Campos XML") }}';
            analyzeBtn.disabled = false;
        });
    }

    function analyzeNumCamValues() {
        const analyzeBtn = document.getElementById('analyzeNumCamBtn');
        analyzeBtn.innerHTML = '<i class="fas fa-sync fa-spin"></i> {{ t("Analisando...") }}';
        analyzeBtn.disabled = true;
        const pathElem = document.getElementById('mosaic_source_path');
        const path = pathElem ? pathElem.value : '';
        fetch('/api/get_xml_tag_values', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: path, tag: 'NUM_CAM' })
        }).then(response => response.json()).then(data => {
            const container = document.getElementById('numCamFilterCheckboxes');
            container.innerHTML = '';
            if (data.values && data.values.length > 0) {
                 data.values.forEach(val => {
                    const isChecked = mosaicConfig.filter_config?.NUM_CAM?.selected_values?.includes(val) ? 'checked' : 'checked';
                    container.innerHTML += `<div class="checkbox-item"><input type="checkbox" name="num_cam_filter" value="${val}" id="filter_cam_${val}" ${isChecked}><label for="filter_cam_${val}">{{ t('Câmara') }} ${val}</label></div>`;
                });
            } else {
                container.innerHTML = `<p style="color: #e74c3c;"><b>Indisponível:</b> Nenhum valor para 'NUM_CAM' encontrado.</p>`;
            }
            analyzeBtn.innerHTML = '<i class="fas fa-camera"></i> {{ t("Analisar Câmeras (NUM_CAM)") }}';
            analyzeBtn.disabled = false;
        }).catch(error => {
            document.getElementById('numCamFilterCheckboxes').innerHTML = `<p style="color:red;">{{ t("Erro ao analisar.") }}</p>`;
            analyzeBtn.innerHTML = '<i class="fas fa-camera"></i> {{ t("Analisar Câmeras (NUM_CAM)") }}';
            analyzeBtn.disabled = false;
        });
    }

    function updateOverlayDropdowns() {
        const selectedFields = Array.from(document.querySelectorAll('input[name="xml_fields"]:checked')).map(el => el.value);
        const overlayDropdowns = ['overlay_top', 'overlay_bottom_left', 'overlay_bottom_right'];
        overlayDropdowns.forEach(dropdownId => {
            const select = document.getElementById(dropdownId);
            const currentVal = mosaicConfig.overlay_config?.[dropdownId.replace('overlay_', '')] || '';
            select.innerHTML = '<option value="">-- {{ t("Nenhum") }} --</option>';
            if (dropdownId === 'overlay_bottom_right') {
                const isSelected = (currentVal === '_file_timestamp_') ? 'selected' : '';
                select.innerHTML += `<option value="_file_timestamp_" ${isSelected}>{{ t("Timestamp do Arquivo (HH:MM)") }}</option>`;
            }
            selectedFields.forEach(field => {
                const isSelected = (field === currentVal) ? 'selected' : '';
                select.innerHTML += `<option value="${field}" ${isSelected}>${field}</option>`;
            });
        });
    }

    document.getElementById('normal_mosaic_config').addEventListener('submit', function(e) {
        e.preventDefault();
        const selectedFieldsWithAliases = {};
        
        document.querySelectorAll('input[name="xml_fields"]:checked').forEach(el => {
            const fieldName = el.value;
            const aliasInput = document.querySelector(`input[name="alias_${fieldName}"]`);
            selectedFieldsWithAliases[fieldName] = aliasInput && aliasInput.value.trim() ? aliasInput.value.trim() : fieldName;
        });
        
        const selectedNumCamFilters = Array.from(document.querySelectorAll('input[name="num_cam_filter"]:checked')).map(el => el.value);
        const availableNumCamFilters = Array.from(document.querySelectorAll('input[name="num_cam_filter"]')).map(el => el.value);
        
        const configData = {
            xml_config: { available_xml_fields: mosaicConfig.xml_config?.available_xml_fields || [], selectedFields: selectedFieldsWithAliases },
            display_config: {
                orientation: parseInt(document.getElementById('normal_orientation').value) || 0,
                grid_columns: parseInt(document.getElementById('grid_columns').value) || 4,
                grid_lines: parseInt(document.getElementById('grid_lines').value) || 3,
                image_size: parseInt(document.getElementById('image_size').value) || 300,
                max_images: parseInt(document.getElementById('max_images').value) || 50,
                refresh_interval: parseInt(document.getElementById('refresh_interval').value) || 30,
                zoom_percentage: parseInt(document.getElementById('zoom_percentage').value) || 250
            },
            overlay_config: { top: document.getElementById('overlay_top').value, bottom_left: document.getElementById('overlay_bottom_left').value, bottom_right: document.getElementById('overlay_bottom_right').value },
            filter_config: { NUM_CAM: { enabled: true, available_values: availableNumCamFilters, selected_values: selectedNumCamFilters } }
        };
        const linhaSelect = document.getElementById('mosaic_line_selector');
        const linha = linhaSelect ? linhaSelect.value : 'global';
        
        fetch('/save_mosaic_config?linha=' + linha, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(configData)
        }).then(response => response.json()).then(res => { showAlert(res.message, res.status === 'success' ? 'success' : 'danger'); }).catch(error => { showAlert('{{ t("Erro de comunicação.") }}', 'danger'); });
    });
    
    document.getElementById('pkirisConfigForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const data = {
            pkiris_retention_days: document.getElementById('pkiris_retention_days').value,
            pkiris_dst_root: document.getElementById('pkiris_dst_root').value,
            linhas: {}
        };
        
        document.querySelectorAll('input[id^="pkiris_src_"]').forEach(input => {
            const parts = input.id.split('_');
            if(parts.length >= 4) {
                const linha = parts[2];
                const maquina = parts[3];
                if (!data.linhas[linha]) data.linhas[linha] = {};
                data.linhas[linha][maquina] = input.value;
            }
        });
        
        fetch('/save_pkiris_config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        }).then(res => res.json()).then(res => {
            showAlert(res.message, res.status === 'success' ? 'success' : 'danger');
        });
    });

    document.getElementById('historicosConfigForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const data = {
            historicos_retention_days: document.getElementById('historicos_retention_days').value,
            historicos_dst_root: document.getElementById('historicos_dst_root').value,
            linhas: {}
        };
        
        document.querySelectorAll('input[id^="historico_src_"]').forEach(input => {
            const parts = input.id.split('_');
            if(parts.length >= 4) {
                const linha = parts[2];
                const maquina = parts[3];
                if (!data.linhas[linha]) data.linhas[linha] = {};
                data.linhas[linha][maquina] = input.value;
            }
        });
        
        fetch('/save_historicos_config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        }).then(res => res.json()).then(res => {
            showAlert(res.message, res.status === 'success' ? 'success' : 'danger');
        });
    });

    document.getElementById('artigosConfigForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const data = {
            artigos_retention_days: document.getElementById('artigos_retention_days').value,
            artigos_dst_root: document.getElementById('artigos_dst_root').value,
            linhas: {}
        };
        
        document.querySelectorAll('input[id^="artigo_src_"]').forEach(input => {
            const parts = input.id.split('_');
            if(parts.length >= 4) {
                const linha = parts[2];
                const maquina = parts[3];
                if (!data.linhas[linha]) data.linhas[linha] = {};
                data.linhas[linha][maquina] = input.value;
            }
        });
        
        fetch('/save_artigos_config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        }).then(res => res.json()).then(res => {
            showAlert(res.message, res.status === 'success' ? 'success' : 'danger');
        });
    });

    function startPkirisBackup() {
        fetch('/start_pkiris', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_pkiris', true);
        });
    }
    function stopPkirisBackup() {
        fetch('/stop_pkiris', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_pkiris', false);
        });
    }

    function startHistoricosBackup() {
        fetch('/start_historicos', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_historicos', true);
        });
    }
    function stopHistoricosBackup() {
        fetch('/stop_historicos', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_historicos', false);
        });
    }

    function startArtigosBackup() {
        fetch('/start_artigos', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_artigos', true);
        });
    }
    function stopArtigosBackup() {
        fetch('/stop_artigos', {method: 'POST'}).then(r => r.json()).then(d => {
            showAlert(d.message, d.status === 'success' ? 'success' : 'danger');
            if (d.status === 'success') updateServiceLED('service_artigos', false);
        });
    }

    function openFileBrowser(inputId) {
        currentInputId = inputId;
        const currentValue = document.getElementById(inputId).value;
        currentPath = currentValue || '/';
        document.getElementById('fileBrowserModal').style.display = 'block';
        loadDirectoryContents(currentPath);
    }

    function closeFileBrowser() {
        document.getElementById('fileBrowserModal').style.display = 'none';
        currentInputId = '';
    }

    function loadDirectoryContents(path) {
        document.getElementById('currentPath').textContent = path;
        document.getElementById('fileList').innerHTML = '<li>{{ t("Carregando...") }}</li>';
        fetch('/browse_directory', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
        }).then(response => response.json()).then(data => {
            if (data.success) {
                if (data.current_path) { currentPath = data.current_path; document.getElementById('currentPath').textContent = currentPath; }
                displayDirectoryContents(data.contents, currentPath);
            } else { document.getElementById('fileList').innerHTML = `<li style="color: red;">{{ t("Erro:") }} ${data.error}</li>`; }
        }).catch(error => { document.getElementById('fileList').innerHTML = `<li style="color: red;">{{ t("Erro de comunicação.") }}</li>`; });
    }

    function displayDirectoryContents(contents, path) {
        const fileList = document.getElementById('fileList');
        fileList.innerHTML = '';
        
        if (path !== '/' && path !== '' && !(path.length === 3 && path.charAt(1) === ':')) {
            let parentPath = path.substring(0, path.lastIndexOf('/'));
            if (!parentPath) parentPath = '/';
            if (parentPath.endsWith(':')) parentPath += '/';
            const parentItem = document.createElement('li');
            parentItem.className = 'file-item parent';
            parentItem.innerHTML = '<i class="fas fa-arrow-up"></i> .. {{ t("(Pasta Pai)") }}';
            parentItem.onclick = () => { currentPath = parentPath; loadDirectoryContents(parentPath); };
            fileList.appendChild(parentItem);
        }
        
        contents.directories.forEach(dir => {
            const item = document.createElement('li');
            item.className = 'file-item folder';
            item.innerHTML = `<i class="fas fa-folder"></i> ${dir}`;
            item.onclick = () => { currentPath = path.endsWith('/') ? `${path}${dir}` : `${path}/${dir}`; loadDirectoryContents(currentPath); };
            fileList.appendChild(item);
        });
        
        contents.files.forEach(file => {
            const item = document.createElement('li');
            item.className = 'file-item';
            item.innerHTML = `<i class="fas fa-file"></i> ${file}`;
            item.style.opacity = '0.6';
            fileList.appendChild(item);
        });
        
        if (contents.directories.length === 0 && contents.files.length === 0) {
            const item = document.createElement('li');
            item.innerHTML = '<i>{{ t("Pasta vazia") }}</i>';
            item.style.fontStyle = 'italic';
            item.style.color = '#999';
            fileList.appendChild(item);
        }
    }

    function selectCurrentPath() {
        if (currentInputId) {
            document.getElementById(currentInputId).value = currentPath;
            checkPathAccess(currentInputId);
            closeFileBrowser();
        }
    }

    function checkPathAccess(inputId) {
        const ledId = 'led_' + inputId;
        const pathElem = document.getElementById(inputId);
        const path = pathElem ? pathElem.value : '';
        const led = document.getElementById(ledId);
        if (!path || path.trim() === '') { if(led) led.className = 'status-led'; return; }
        if(led) led.className = 'status-led checking';
        fetch('/check_path_access', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
        }).then(response => response.json()).then(data => {
            if (data.accessible) { if(led) { led.className = 'status-led online'; led.title = '{{ t("Caminho acessível") }}'; } } 
            else { if(led) { led.className = 'status-led'; led.title = `{{ t("Não acessível") }}`; } }
        }).catch(error => { if(led) { led.className = 'status-led'; led.title = '{{ t("Erro ao verificar") }}'; } });
    }

    function checkAllPaths() {
        const pathInputs = document.querySelectorAll('input[type="text"]');
        pathInputs.forEach(input => { if (input.value && input.value.trim() !== '' && input.id) checkPathAccess(input.id); });
    }

    function updateServiceLED(ledId, isActive) {
        const led = document.getElementById(ledId);
        if (led) led.className = isActive ? 'service-led active' : 'service-led inactive';
    }

    function updateAllServiceStatus() {
        fetch('/service_status').then(response => response.json()).then(data => {
            for (const [key, active] of Object.entries(data.backup_services || {})) updateServiceLED(`service_backup_${key}`, active);
            for (const [key, status] of Object.entries(data.mosaic_process_status || {})) updateServiceLED(`service_mosaic_${key}`, status.running);
            updateServiceLED('service_file_copying', data.file_copying_active || false);
            updateServiceLED('service_mirror_ssd', data.mirror_ssd_active || false);
            updateServiceLED('service_pkiris', data.pkiris_active || false);
            updateServiceLED('service_historicos', data.historicos_active || false);
            updateServiceLED('service_artigos', data.artigos_active || false);
        }).catch(error => {});
    }

    function loadServiceStatusAndCounters() {
        fetch('/get_line_status').then(response => response.json()).then(data => {
            const tableBody = document.getElementById('service-status-table-body');
            tableBody.innerHTML = '';
            data.lines.forEach(line => {
                const row = document.createElement('tr');
                const backupStatus = data.backup_services[line.key] ? 'active' : 'inactive';
                const mosaicStatus = data.mosaic_services[line.key] ? 'active' : 'inactive';
                const shift_jpg = data.shift_counters[line.key]?.jpg || 0;
                const shift_xml = data.shift_counters[line.key]?.xml || 0;
                const day_jpg = data.day_counters[line.key]?.jpg || 0;
                const day_xml = data.day_counters[line.key]?.xml || 0;
                
                const configStatus = mosaicStatus === 'active' ? '<span class="status-indicator status-running">{{ t("Ativo") }}</span>' : '<span class="status-indicator status-stopped">{{ t("Inativo") }}</span>';
                const processStatus = mosaicStatus === 'active' ? '<span class="status-indicator status-running">{{ t("Executando") }}</span>' : '<span class="status-indicator status-stopped">{{ t("Parado") }}</span>';
                
                const url = `<a href="http://${window.location.hostname}:` + (data.mosaic_services[line.key] ? data.mosaic_services[line.key].port || '#' : '#') + `" target="_blank" style="color: #3498db; text-decoration: underline;">{{ t("Abrir") }}</a>`;
                
                row.innerHTML = `<td>${line.linha}</td><td>${line.maquina_display}</td><td><span class="service-led ${backupStatus}"></span></td><td><span class="service-led ${mosaicStatus}"></span></td><td>${shift_jpg} / ${shift_xml}</td><td>${day_jpg} / ${day_xml}</td>`;
                tableBody.appendChild(row);
            });
        }).catch(error => {  });
    }
    
    function loadMosaicStatus() {
        const tableBody = document.getElementById('mosaic-status-table-body');
        tableBody.innerHTML = '<tr><td colspan="8" style="text-align:center;">{{ t("Carregando status...") }}</td></tr>';
        fetch('/api/mosaic_status').then(response => response.json()).then(data => {
            tableBody.innerHTML = '';
            if (Object.keys(data.mosaics).length === 0) { tableBody.innerHTML = '<tr><td colspan="8" style="text-align:center;">{{ t("Nenhuma máquina configurada.") }}</td></tr>'; return; }
            for (const key in data.mosaics) {
                const status = data.mosaics[key];
                const row = document.createElement('tr');
                const configStatus = status.is_configured ? '<span class="status-indicator status-running">{{ t("Ativo") }}</span>' : '<span class="status-indicator status-stopped">{{ t("Inativo") }}</span>';
                const processStatus = status.is_running ? '<span class="status-indicator status-running">{{ t("Executando") }}</span>' : '<span class="status-indicator status-stopped">{{ t("Parado") }}</span>';
                
                const url = status.port ? `<a href="http://${window.location.hostname}:${status.port}" target="_blank" style="color: #3498db; font-weight: bold; text-decoration: underline;">{{ t("Abrir") }}</a>` : 'N/A';
                
                const actions = `
                    <div class="actions">
                       <button class="btn btn-sm btn-success" title="{{ t('Iniciar') }}" onclick="controlMosaic('${status.linha}', '${status.maquina}', 'start')" ${status.is_running ? 'disabled' : ''}><i class="fas fa-play"></i></button>
                       <button class="btn btn-sm btn-danger" title="{{ t('Parar') }}" onclick="controlMosaic('${status.linha}', '${status.maquina}', 'stop')" ${!status.is_running ? 'disabled' : ''}><i class="fas fa-stop"></i></button>
                       <button class="btn btn-sm btn-warning" title="{{ t('Reiniciar') }}" onclick="controlMosaic('${status.linha}', '${status.maquina}', 'restart')" ${!status.is_running ? 'disabled' : ''}><i class="fas fa-sync"></i></button>
                    </div>`;
                row.innerHTML = `<td>${status.linha}</td><td>${status.maquina_display}</td><td>${configStatus}</td><td>${processStatus}</td><td>${status.pid || 'N/A'}</td><td>${status.port || 'N/A'}</td><td>${url}</td><td>${actions}</td>`;
                tableBody.appendChild(row);
            }
        }).catch(error => { tableBody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:red;">{{ t("Erro ao carregar status.") }}</td></tr>`; });
    }

    function startFileCopying() {
        fetch('/start_file_copying', {method: 'POST'}).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            if (data.status === 'success') { updateServiceLED('service_file_copying', true); updateCopyStatus(); }
        });
    }

    function startMirrorSSD() {
        fetch('/start_mirror_ssd', {method: 'POST'}).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            if (data.status === 'success') updateServiceLED('service_mirror_ssd', true);
        });
    }

    function stopAllServices() {
        fetch('/stop_all_services', {method: 'POST'}).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            if (data.status === 'success') {
                document.querySelectorAll('.service-led').forEach(led => { led.className = 'service-led inactive'; });
                if (document.getElementById('gestao_mosaico').classList.contains('active')) loadMosaicStatus();
            }
        });
    }

    function updateCopyStatus() {
        fetch('/copy_status').then(response => response.json()).then(data => {
            const statusDiv = document.getElementById('copy-status');
            if (data.running) statusDiv.innerHTML = `<p><strong>{{ t("Sistema de cópia ativo") }}</strong></p><p>{{ t("Threads ativos:") }} ${data.active_threads}</p>`;
            else statusDiv.innerHTML = '<p>{{ t("Sistema de cópia parado") }}</p>';
            updateServiceLED('service_file_copying', data.running);
        });
    }

    document.getElementById('configGeralForm').addEventListener('submit', function (e) {
        e.preventDefault();
        const articlePathElem = document.getElementById('article_analysis_path');
        const data = {
            ssd_path: document.getElementById('ssd_path').value,
            mirror_source_path: document.getElementById('mirror_source_path').value,
            mosaic_config_folder: document.getElementById('mosaic_config_folder').value,
            article_analysis_path: articlePathElem ? articlePathElem.value : "",
            log_file_path: document.getElementById('log_file_path').value,
            mirror_include_subfolders: document.getElementById('mirror_include_subfolders').checked,
            ssd_retention_days: document.getElementById('ssd_retention_days').value,
            hdd_retention_months: document.getElementById('hdd_retention_months').value,
            scan_interval_sec: document.getElementById('scan_interval_sec').value,
            turnos: {
                turno1: { inicio: document.getElementById('turno1_inicio').value, fim: document.getElementById('turno1_fim').value },
                turno2: { inicio: document.getElementById('turno2_inicio').value, fim: document.getElementById('turno2_fim').value },
                turno3: { inicio: document.getElementById('turno3_inicio').value, fim: document.getElementById('turno3_fim').value }
            }
        };
        fetch('/save_general_config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        }).then(response => response.json()).then(data => { showAlert(data.message, data.status === 'success' ? 'success' : 'danger'); });
    });

    function startArticleAnalysis() {
        const pathElem = document.getElementById('article_analysis_path');
        const path = pathElem ? pathElem.value : "";
        fetch('/api/start_article_analysis', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: path})
        }).then(r => r.json()).then(d => {
            showAlert(d.message, d.status);
            if (d.status === 'success') {
                document.getElementById('articleAnalysisProgress').style.display = 'block';
                pollArticleAnalysis();
            }
        });
    }
    
    function resetArticleAnalysis() {
        if(confirm('{{ t("Isto apagará a cache de análise anterior e lerá tudo novamente. Confirmar?") }}')) {
            const pathElem = document.getElementById('article_analysis_path');
            const path = pathElem ? pathElem.value : "";
            fetch('/api/reset_article_analysis', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path})
            }).then(r => r.json()).then(d => {
                showAlert(d.message, d.status);
                if (d.status === 'success') {
                    document.getElementById('articleAnalysisProgress').style.display = 'block';
                    pollArticleAnalysis();
                }
            });
        }
    }
    function stopArticleAnalysis() {
        fetch('/api/stop_article_analysis', {method: 'POST'})
            .then(r => r.json()).then(d => showAlert(d.message, d.status));
    }
    function pollArticleAnalysis() {
        if (aaInterval) return;
        document.getElementById('articleAnalysisProgress').style.display = 'block';
        aaInterval = setInterval(() => {
            fetch('/api/status_article_analysis').then(r => r.json()).then(data => {
                document.getElementById('aa_progress_bar').style.width = data.progress + '%';
                
                let statusTxt = '';
                if (data.status === 'idle') {
                    statusTxt = '{{ t("Pausado") }}';
                    document.getElementById('aa_progress_text').innerText = '{{ t("A aguardar início...") }}';
                }
                else if (data.status === 'scanning_dirs') {
                    statusTxt = '{{ t("A procurar pastas e ficheiros XML...") }}';
                    document.getElementById('aa_progress_text').innerText = '{{ t("A preparar...") }}';
                }
                else if (data.status === 'counting_files') {
                    statusTxt = '{{ t("A indexar ficheiros (encontrados: ") }}' + data.total_files + ')...';
                    document.getElementById('aa_progress_text').innerText = '{{ t("A organizar ") }}' + data.total_files + ' {{ t("ficheiros...") }}';
                }
                else if (data.status === 'processing') {
                    statusTxt = '{{ t("A analisar ficheiros XML (Multi-Thread)...") }}';
                    document.getElementById('aa_progress_text').innerText = '{{ t("Analisados ") }}' + data.files_done + ' {{ t("ficheiros de um total de ") }}' + data.total_files;
                }
                else if (data.status === 'completed') {
                    statusTxt = '{{ t("Análise Concluída!") }}';
                    document.getElementById('aa_progress_text').innerText = '{{ t("Análise de ") }}' + data.total_files + ' {{ t("ficheiros concluída!") }}';
                }
                else if (data.status === 'interrupted') {
                    statusTxt = '{{ t("Análise Interrompida.") }}';
                    document.getElementById('aa_progress_text').innerText = '{{ t("Interrompido nos ") }}' + data.files_done + ' {{ t("de ") }}' + data.total_files + ' {{ t("ficheiros.") }}';
                }
                
                document.getElementById('aa_status_text').innerText = statusTxt;
                document.getElementById('aa_eta_text').innerText = "{{ t('ETA: ') }}" + data.eta;
                
                const logWin = document.getElementById('aa_log_window');
                if (data.recent_logs && data.recent_logs.length > 0) {
                    logWin.innerHTML = data.recent_logs.map(l => `<p>${l}</p>`).join('');
                    logWin.scrollTop = logWin.scrollHeight;
                }
                
                if (data.current_file) {
                    document.getElementById('aa_current_file').innerText = "{{ t('Atual: ') }}" + data.current_file;
                }
                
                if (!data.running && data.status !== 'scanning_dirs' && data.status !== 'processing' && data.status !== 'counting_files') {
                    clearInterval(aaInterval);
                    aaInterval = null;
                }
            });
        }, 1500);
    }

    function toggleBackup(linha, maquina) {
        fetch('/toggle_backup', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({linha: linha, maquina: maquina})
        }).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            if (data.status === 'success') updateServiceLED(`service_backup_${linha}_${maquina}`, data.active);
        });
    }

    function toggleMosaico(linha, maquina) {
        fetch('/toggle_mosaic', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({linha: linha, maquina: maquina})
        }).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            if (data.status === 'success') {
                updateServiceLED(`service_mosaic_${linha}_${maquina}`, data.active);
                if (document.getElementById('gestao_mosaico').classList.contains('active')) setTimeout(loadMosaicStatus, 1000);
            }
        });
    }

    function controlMosaic(linha, maquina, action) {
        fetch(`/api/mosaic_control/${action}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({linha: linha, maquina: maquina})
        }).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            setTimeout(loadMosaicStatus, 500); 
        });
    }

    function saveAllConfig() {
        const config = { linhas: {} };
        document.querySelectorAll('.linha-card').forEach(card => {
            const h4 = card.querySelector('h4');
            const linhaMatch = h4.innerText.match(/Linha ([0-9]+)/);
            if (!linhaMatch) return;
            const linha = linhaMatch[1];
            if (!config.linhas[linha]) config.linhas[linha] = {};
            
            const cycleModeInput = document.getElementById(`cycle_mode_active_${linha}`);
            if (cycleModeInput) config.linhas[linha].cycle_mode_active = cycleModeInput.checked;
            
            const cycleTimeInput = document.getElementById(`cycle_time_sec_${linha}`);
            if (cycleTimeInput) config.linhas[linha].cycle_time_sec = parseInt(cycleTimeInput.value) || 30;
            
            const testModeInput = document.getElementById(`use_test_mode_${linha}`);
            if (testModeInput) config.linhas[linha].use_test_mode = testModeInput.checked;

            card.querySelectorAll('.machine-section').forEach(section => {
                const header = section.querySelector('h5, h6');
                if (!header || header.innerHTML.includes('fa-sync-alt')) return; 
                
                let maquina = '';
                const headerText = header.textContent || header.innerText;
                
                if (linha !== '34') {
                    if (headerText.includes('Lateral') || headerText.includes('Lateral'.toUpperCase())) maquina = 'lateral';
                    else if (headerText.includes('Fundo') || headerText.includes('Fundo'.toUpperCase()) || headerText.includes('Topo e Fundo')) maquina = 'fundo';
                } else {
                    const machineMatch = headerText.match(/(lateral|fundo)\s*([0-9])/i);
                    if (machineMatch) {
                        maquina = machineMatch[1].toLowerCase() + machineMatch[2];
                    }
                }
                
                if (!maquina) return;
                if (!config.linhas[linha][maquina]) config.linhas[linha][maquina] = {};
                
                const srcProdInput = document.getElementById(`origem_prod_${linha}_${maquina}`);
                const dstProdInput = document.getElementById(`destino_prod_${linha}_${maquina}`);
                const srcTestInput = document.getElementById(`origem_test_${linha}_${maquina}`);
                const dstTestInput = document.getElementById(`destino_test_${linha}_${maquina}`);
                
                const deleteInput = document.getElementById(`delete_source_${linha}_${maquina}`);
                const portInput = document.getElementById(`mosaic_port_${linha}_${maquina}`);

                if(srcProdInput) config.linhas[linha][maquina].src_prod = srcProdInput.value;
                if(dstProdInput) config.linhas[linha][maquina].dst_prod = dstProdInput.value;
                if(srcTestInput) config.linhas[linha][maquina].src_test = srcTestInput.value;
                if(dstTestInput) config.linhas[linha][maquina].dst_test = dstTestInput.value;
                
                if(deleteInput) config.linhas[linha][maquina].delete_source = deleteInput.checked;
                if(portInput) config.linhas[linha][maquina].mosaic_port = parseInt(portInput.value) || 0;
            });
        });
        
        const vgPortLat = document.getElementById('overview_port_lateral');
        const vgPortFun = document.getElementById('overview_port_fundo');
        const vgCycle = document.getElementById('overview_cycle_mode_active');
        const vgTime = document.getElementById('overview_cycle_time_sec');
        const visao_global = {
            port_lateral: vgPortLat ? (parseInt(vgPortLat.value) || 5098) : 5098,
            port_fundo: vgPortFun ? (parseInt(vgPortFun.value) || 5099) : 5099,
            cycle_mode_active: vgCycle ? vgCycle.checked : true,
            cycle_time_sec: vgTime ? (parseInt(vgTime.value) || 30) : 30
        };
        
        fetch('/save_lines_config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({linhas: config.linhas, visao_global: visao_global})
        }).then(response => response.json()).then(data => { showAlert(data.message, data.status === 'success' ? 'success' : 'danger'); });
    }

    function startAll() {
        fetch('/start_all', {method: 'POST'}).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            setTimeout(updateAllServiceStatus, 2000);
        });
    }

    function stopAll() {
        fetch('/stop_all', {method: 'POST'}).then(response => response.json()).then(data => {
            showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            setTimeout(updateAllServiceStatus, 2000);
        });
    }

    function loadMachinesList() {
        fetch('/get_machines_list').then(response => response.json()).then(data => {
            const machinesList = document.getElementById('machines-list');
            let html = '';
            data.machines.forEach(machine => {
                html += `<div class="toggle-item"><span>{{ t('Linha') }} ${machine.linha} - ${machine.maquina_display}</span><label class="switch"><input type="checkbox" name="machines" value="${machine.key}" id="export_machine_${machine.key}"><span class="slider"></span></label></div>`;
            });
            machinesList.innerHTML = html;
        });
    }

    document.getElementById('exportForm').addEventListener('submit', function (e) {
        e.preventDefault();
        const formData = new FormData(this);
        const selectedTurnos = Array.from(document.querySelectorAll('input[name="turnos"]:checked')).map(cb => cb.value);
        const selectedMachines = Array.from(document.querySelectorAll('input[name="machines"]:checked')).map(cb => cb.value);
        if (selectedTurnos.length === 0 || selectedMachines.length === 0) { showAlert('{{ t("Por favor, selecione pelo menos um turno e uma máquina.") }}', 'warning'); return; }
        const exportData = { export_date: formData.get('export_date'), turnos: selectedTurnos, machines: selectedMachines, compress: formData.has('compress') };
        const progressContainer = document.getElementById('exportProgress');
        const progressBar = document.getElementById('progressBar');
        const progressText = document.getElementById('progressText');
        const submitBtn = document.getElementById('btn-export-submit');
        
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressBar.innerText = '0%';
        progressText.innerHTML = '{{ t("A iniciar compilação...") }}';
        submitBtn.disabled = true;

        fetch('/api/export_start', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(exportData)
        }).then(res => res.json()).then(data => {
            if (data.task_id) checkExportStatus(data.task_id, submitBtn);
            else throw new Error('{{ t("Erro ao iniciar a tarefa de exportação.") }}');
        }).catch(error => {
            showAlert('{{ t("Erro:") }} ' + error.message, 'danger');
            progressContainer.style.display = 'none';
            submitBtn.disabled = false;
        });
    });

    function checkExportStatus(taskId, submitBtn) {
        fetch(`/api/export_status/${taskId}`).then(res => res.json()).then(data => {
            const progressBar = document.getElementById('progressBar');
            const progressText = document.getElementById('progressText');
            const progressContainer = document.getElementById('exportProgress');
            if (data.status === 'processing') {
                progressBar.style.width = data.progress + '%';
                progressBar.innerText = data.progress + '%';
                progressText.innerHTML = `{{ t("A processar imagens...") }}`;
                setTimeout(() => checkExportStatus(taskId, submitBtn), 1000);
            } else if (data.status === 'completed') {
                progressBar.style.width = '100%';
                progressBar.innerText = '100%';
                progressText.innerHTML = '{{ t("Concluído! A iniciar transferência...") }}';
                window.location.href = `/api/export_download/${taskId}`;
                setTimeout(() => { progressContainer.style.display = 'none'; submitBtn.disabled = false; showAlert('{{ t("Exportação concluída!") }}', 'success'); }, 3000);
            } else {
                showAlert(`{{ t("Erro:") }} ${data.message}`, 'danger');
                progressContainer.style.display = 'none';
                submitBtn.disabled = false;
            }
        }).catch(err => {
            showAlert('{{ t("Erro de comunicação.") }}', 'danger');
            document.getElementById('exportProgress').style.display = 'none';
            submitBtn.disabled = false;
        });
    }

    function loadDiagnostics() {
        fetch('/diagnostics').then(response => response.json()).then(data => {
            document.getElementById('current-shift').textContent = data.current_shift;
            document.getElementById('volume1-usage').textContent = data.storage.volume1.percent + '%';
            document.getElementById('volume2-usage').textContent = data.storage.volume2.percent + '%';
            document.getElementById('system-load').textContent = data.system.cpu_percent + '%';
            document.getElementById('memory-usage').textContent = data.system.memory_percent + '%';
            document.getElementById('files-copied-shift').textContent = `${data.total_shift_jpg} / ${data.total_shift_xml}`;
            document.getElementById('files-copied-day').textContent = `${data.total_day_jpg} / ${data.total_day_xml}`;
            let servicesHtml = '';
            for (const [service, status] of Object.entries(data.services)) {
                const statusClass = status ? 'status-running' : 'status-stopped';
                const statusText = status ? '{{ t("EXECUTANDO") }}' : '{{ t("PARADO") }}';
                servicesHtml += `<div class="status-indicator ${statusClass}"><i class="fas fa-circle"></i> ${service}: ${statusText}</div>`;
            }
            document.getElementById('services-status').innerHTML = servicesHtml;
        });
    }

    function checkServices() {
        fetch('/service_status').then(response => response.json()).then(data => {
            showAlert('{{ t("Check de serviços registrado no log") }}', 'info');
            updateAllServiceStatus();
        });
    }

    function loadDirectories() {
        fetch('/list_directories').then(response => response.json()).then(data => {
            let html = '<ul>';
            data.directories.forEach(dir => { html += `<li>${dir}</li>`; });
            html += '</ul>';
            document.getElementById('directories-list').innerHTML = html;
        });
    }

    function loadConnectedIPs() {
        fetch('/connected_ips').then(response => response.json()).then(data => {
            let html = '';
            data.ips.forEach(ip => { 
                html += `<div class="ip-item">
                            <span><strong>${ip.ip}</strong> <span style="color:#7f8c8d; font-size:0.85rem;">- ${ip.page}</span></span>
                            <div>
                                <span class="ip-status ${ip.status}">${ip.status.toUpperCase()}</span>
                                <button class="btn btn-sm btn-warning" style="margin-left: 10px;" onclick="restartTerminal('${ip.ip}')"><i class="fas fa-power-off"></i> {{ t('Reiniciar') }}</button>
                            </div>
                         </div>`; 
            });
            document.getElementById('connected-ips').innerHTML = html;
        });
    }
    
    function restartTerminal(ip) {
        if(confirm(`{{ t('Tem a certeza que deseja reiniciar o terminal remoto no IP:') }} ${ip}?`)) {
            fetch('/api/restart_terminal', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ip: ip})
            })
            .then(r => r.json())
            .then(data => {
                showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
            })
            .catch(e => showAlert('{{ t("Erro de comunicação.") }}', 'danger'));
        }
    }
    
    function loadUserList() {
        fetch('/api/users').then(r => r.json()).then(data => {
            const tableBody = document.getElementById('user-list-body');
            tableBody.innerHTML = '';
            data.users.forEach(user => {
                const row = document.createElement('tr');
                let actions = '';
                if (IS_DEV && user.username !== 'cid') {
                    actions = `<button class="btn btn-sm btn-danger" onclick="deleteUser('${user.username}')"><i class="fas fa-trash"></i> {{ t('Apagar') }}</button>`;
                }
                const userTypeStr = user.is_dev ? '{{ t("Developer") }}' : '{{ t("Normal") }}';
                row.innerHTML = `<td>${user.username}</td><td>${userTypeStr}</td><td class="actions">${actions}</td>`;
                tableBody.appendChild(row);
            });
        });
    }

    document.getElementById('createUserForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const username = this.new_username.value;
        const password = this.new_password.value;
        const confirm = this.confirm_password.value;
        if (password !== confirm) { showAlert('{{ t("Passwords não coincidem.") }}', 'danger'); return; }
        fetch('/api/users/create', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username, password})
        }).then(r => r.json()).then(data => {
            showAlert(data.message, data.status);
            if(data.status === 'success') { this.reset(); loadUserList(); }
        });
    });
    
    document.getElementById('changePasswordForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const currentPassword = this.current_password ? this.current_password.value : '';
        const newPassword = this.new_password.value;
        const confirmPassword = this.confirm_password.value;
        if (newPassword !== confirmPassword) { showAlert('{{ t("As novas passwords não coincidem.") }}', 'danger'); return; }
        fetch('/api/users/change_password', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
        }).then(r => r.json()).then(data => {
            showAlert(data.message, data.status);
            if (data.status === 'success') this.reset();
        });
    });

    function deleteUser(username) {
        if (confirm(`{{ t('Tem a certeza que quer apagar o utilizador ') }}${username}?`)) {
            fetch('/api/users/delete', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username})
            }).then(r => r.json()).then(data => {
                showAlert(data.message, data.status);
                if (data.status === 'success') loadUserList();
            });
        }
    }

    function refreshLogs() {
        fetch('/logs').then(response => {
            if (!response.ok) throw new Error('{{ t("Falha no servidor ao ler o ficheiro.") }}');
            return response.json();
        }).then(data => {
            const escapedLogs = data.logs.map(line => line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"));
            document.getElementById('logs-content').innerHTML = escapedLogs.join('<br>');
            document.getElementById('logs-content').scrollTop = document.getElementById('logs-content').scrollHeight;
        }).catch(error => { document.getElementById('logs-content').innerHTML = '<span style="color:#e74c3c;">{{ t("Erro ao carregar logs") }}</span>'; });
    }

    function clearLogs() {
        const confirmation = prompt('{{ t("Tem certeza que deseja limpar todos os logs? Digite sim para confirmar.") }}');
        if (confirmation && confirmation.toLowerCase() === 'sim') {
            fetch('/clear_logs', {method: 'POST'}).then(response => response.json()).then(data => {
                showAlert(data.message, data.status === 'success' ? 'success' : 'danger');
                if (data.status === 'success') refreshLogs();
            });
        }
    }

    function downloadLogs() { window.open('/download_logs', '_blank'); }

    function showAlert(message, type) {
        let messageContainer = document.querySelector('.message-container');
        if (!messageContainer) {
            messageContainer = document.createElement('div');
            messageContainer.className = 'message-container';
            document.body.appendChild(messageContainer);
        }
        const alertDiv = document.createElement('div');
        alertDiv.className = `alert alert-${type}`;
        alertDiv.innerHTML = `<i class="fas fa-${type === 'success' ? 'check' : 'exclamation'}-circle"></i> ${message}`;
        messageContainer.insertBefore(alertDiv, messageContainer.firstChild);
        setTimeout(() => { if (alertDiv.parentNode) alertDiv.parentNode.removeChild(alertDiv); }, 5000);
        alertDiv.addEventListener('click', () => { if (alertDiv.parentNode) alertDiv.parentNode.removeChild(alertDiv); });
    }

    window.onclick = function (event) {
        const modal = document.getElementById('fileBrowserModal');
        if (event.target === modal) closeFileBrowser();
    }

    document.addEventListener('DOMContentLoaded', function () {
        setTimeout(function () { loadConfigurationsFromServer(); }, 1500);
        setTimeout(checkAllPaths, 1000);
        setTimeout(updateAllServiceStatus, 1500);

        setInterval(() => {
            const activeTab = document.querySelector('.tab-content.active');
            if (!activeTab) return;
            switch(activeTab.id) {
                case 'configuracao': updateAllServiceStatus(); break;
                case 'diagnostics': loadDiagnostics(); break;
                case 'servicos': loadServiceStatusAndCounters(); break;
                case 'gestao_mosaico': loadMosaicStatus(); break;
            }
        }, 15000);
    });
</script>

</body>
</html>
"""

# ==============================================================================
# LÓGICA DE BACKEND E GESTÃO DE FICHEIROS
# ==============================================================================

def format_eta(seconds):
    if seconds < 0: return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}h {m}m"
    return f"{m}m {s}s"

def register_article_appearance(linha, date_str, article_name):
    db_path = os.path.join(DATA_DIR, "artigos.json")
    with article_db_lock:
        try:
            db = {}
            if os.path.exists(db_path):
                with open(db_path, 'r', encoding='utf-8') as f:
                    try:
                        db = json.load(f)
                    except json.JSONDecodeError:
                        db = {}
            
            if article_name not in db:
                db[article_name] = []
            
            intervals = db[article_name]
            found_open = False
            
            for interval in intervals:
                if interval["linha"] == linha:
                    try:
                        end_date = datetime.strptime(interval["fim"], "%Y-%m-%d")
                        cur_date = datetime.strptime(date_str, "%Y-%m-%d")
                        delta = (cur_date - end_date).days
                        
                        if 0 <= delta <= 4:
                            interval["fim"] = date_str
                            found_open = True
                            break
                        elif -4 <= delta < 0:
                            start_date = datetime.strptime(interval["inicio"], "%Y-%m-%d")
                            if cur_date < start_date:
                                interval["inicio"] = date_str
                            found_open = True
                            break
                    except Exception:
                        pass
                        
            if not found_open:
                db[article_name].append({"linha": linha, "inicio": date_str, "fim": date_str})
                
            temp_path = db_path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(db, f, ensure_ascii=False, indent=4)
            os.replace(temp_path, db_path)
            
        except Exception as e:
            logging.error(f"Erro ao auto-registar artigo {article_name} na linha {linha}: {e}")

def article_analysis_worker(reset=False, explicit_path=""):
    global analysis_status
    analysis_status['running'] = True
    analysis_status['stop_flag'] = False
    analysis_status['status'] = 'scanning_dirs'
    analysis_status['progress'] = 0
    analysis_status['total_files'] = 0
    analysis_status['files_done'] = 0
    analysis_status['current_file'] = ''
    analysis_status['recent_logs'] = []
    
    def log_msg(msg):
        analysis_status['recent_logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(analysis_status['recent_logs']) > 15:
            analysis_status['recent_logs'].pop(0)

    config = load_config()
    state_file = os.path.join(DATA_DIR, 'article_scan_state.json')
    out_file = os.path.join(DATA_DIR, 'artigos.json')

    if reset:
        state = {"processed_dates": {}, "results": {}}
        log_msg(_t("A reiniciar base de dados de artigos (Reset Forçado)."))
    else:
        state = safe_load_json(state_file, {"processed_dates": {}, "results": {}})
        log_msg(_t("A carregar estado anterior da análise para retoma."))

    analysis_path = explicit_path if explicit_path else config.get('article_analysis_path', '').strip()
    
    if not analysis_path or not os.path.exists(analysis_path):
        log_msg(_t("ERRO: Pasta raiz para análise de artigos não foi fornecida ou não existe."))
        analysis_status['status'] = 'interrupted'
        analysis_status['running'] = False
        return

    linhas_keys = list(config.get('linhas', {}).keys())

    def get_linha_from_path(path):
        parts = path.replace('\\', '/').split('/')
        for part in reversed(parts):
            m = re.search(r'(?:linha|l)[\s_]*(\d{2,3})', part, re.IGNORECASE)
            if m and m.group(1) in linhas_keys:
                return m.group(1)
        for key in linhas_keys:
            if key in parts or f"Linha {key}" in parts or f"Linha_{key}" in parts:
                return key
        return "Desconhecida"

    tasks = {}
    log_msg(f"{_t('A percorrer pasta mestre à procura de datas:')} {analysis_path}")
    
    try:
        for root, dirs, files in os.walk(analysis_path):
            if analysis_status['stop_flag']: break
            basename = os.path.basename(root)
            if re.match(r"^\d{4}-\d{2}-\d{2}$", basename):
                linha = get_linha_from_path(root)
                if linha != "Desconhecida":
                    if linha not in tasks: tasks[linha] = set()
                    tasks[linha].add((root, basename))
                dirs.clear() 
    except Exception as e:
        log_msg(f"Erro ao ler diretoria mestre {analysis_path}: {e}")

    grouped_tasks = {}
    for linha, dates_set in tasks.items():
        processed = set(state["processed_dates"].get(linha, []))
        for d_path, d_str in dates_set:
            if d_str not in processed:
                if linha not in grouped_tasks:
                    grouped_tasks[linha] = {}
                if d_str not in grouped_tasks[linha]:
                    grouped_tasks[linha][d_str] = []
                grouped_tasks[linha][d_str].append(d_path)

    if not grouped_tasks:
        analysis_status['progress'] = 100
        analysis_status['status'] = 'completed'
        analysis_status['eta'] = '00:00:00'
        analysis_status['running'] = False
        log_msg(_t("Nenhuma data nova por analisar. Análise concluída."))
        return

    analysis_status['status'] = 'counting_files'
    dates_grouped = {}
    total_xmls = 0
    
    for linha, date_dict in grouped_tasks.items():
        if analysis_status['stop_flag']: break
        for d_str, paths in date_dict.items():
            if analysis_status['stop_flag']: break
            k = (linha, d_str)
            if k not in dates_grouped: dates_grouped[k] = []
            
            log_msg(f"A contar ficheiros da Linha {linha} - Dia {d_str}...")
            for d_path in paths:
                if analysis_status['stop_flag']: break
                try:
                    with os.scandir(d_path) as it:
                        for entry in it:
                            if entry.is_file() and entry.name.lower().endswith('.xml'):
                                dates_grouped[k].append(entry.path)
                                total_xmls += 1
                    analysis_status['total_files'] = total_xmls
                except Exception:
                    pass

    if total_xmls == 0:
        analysis_status['progress'] = 100
        analysis_status['status'] = 'completed'
        analysis_status['eta'] = '00:00:00'
        analysis_status['running'] = False
        log_msg(_t("Nenhum ficheiro XML encontrado nas pastas indicadas."))
        return

    analysis_status['status'] = 'processing'
    log_msg(f"{_t('Descoberta concluída. Iniciando leitura rápida de')} {total_xmls} {_t('ficheiros XML...')}")
    
    files_done = 0
    start_time = time.time()
    
    def process_xml(filepath):
        if analysis_status['stop_flag']: return None
        analysis_status['current_file'] = filepath
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                chunk = f.read(2048)
                m = REGEX_NOM_ART.search(chunk)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
        return None

    for (linha, d_str), filepaths in dates_grouped.items():
        if analysis_status['stop_flag']: break
        if not filepaths: continue
        
        art_counts_for_day = set()
        log_msg(f"A ler {len(filepaths)} ficheiros (Linha {linha} - {d_str})...")
        
        # Limite reduzido para 8 threads para não sobrecarregar a DS423 com pedidos de disco
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_file = {executor.submit(process_xml, fp): fp for fp in filepaths}
            for future in concurrent.futures.as_completed(future_to_file):
                if analysis_status['stop_flag']: break
                art = future.result()
                if art:
                    art_counts_for_day.add(art)
                
                files_done += 1
                if files_done % 100 == 0 or files_done == total_xmls:
                    analysis_status['files_done'] = files_done
                    analysis_status['progress'] = int((files_done / total_xmls) * 100)
                    elapsed = time.time() - start_time
                    eta_seconds = (elapsed / files_done) * (total_xmls - files_done)
                    analysis_status['eta'] = format_eta(eta_seconds)

        if analysis_status['stop_flag']: break
        
        for art in art_counts_for_day:
            if art not in state["results"]:
                state["results"][art] = []
                
            intervals = state["results"][art]
            found_open = False
            for interval in intervals:
                if interval["linha"] == linha:
                    try:
                        end_date = datetime.strptime(interval["fim"], "%Y-%m-%d")
                        cur_date = datetime.strptime(d_str, "%Y-%m-%d")
                        delta = (cur_date - end_date).days
                        if 0 <= delta <= 4:
                            interval["fim"] = d_str
                            found_open = True
                            break
                    except Exception: pass
            if not found_open:
                state["results"][art].append({"linha": linha, "inicio": d_str, "fim": d_str})
        
        if linha not in state["processed_dates"]:
            state["processed_dates"][linha] = []
        if d_str not in state["processed_dates"][linha]:
            state["processed_dates"][linha].append(d_str)
            
        safe_save_json(state_file, state)
        safe_save_json(out_file, state["results"])

    if not analysis_status['stop_flag']:
        analysis_status['progress'] = 100
        analysis_status['status'] = 'completed'
        analysis_status['eta'] = '00:00:00'
        log_msg(_t("Análise concluída com sucesso."))
    else:
        analysis_status['status'] = 'interrupted'
        log_msg(_t("Análise interrompida pelo utilizador."))

    analysis_status['running'] = False

@app.route('/api/start_article_analysis', methods=['POST'])
@login_required
def start_article_analysis():
    global analysis_thread
    
    data = request.get_json() or {}
    analysis_path = data.get('path', '').strip()
    
    if not analysis_path or not os.path.exists(analysis_path):
        return jsonify({'status': 'danger', 'message': f"{_t('A pasta')} '{analysis_path}' {_t('não está acessível.')}"})
        
    config = load_config()
    if config.get('article_analysis_path') != analysis_path:
        config['article_analysis_path'] = analysis_path
        save_config(config)
        
    if analysis_status['running']:
        return jsonify({'status': 'warning', 'message': _t('Análise já está em curso.')})
    
    analysis_thread = threading.Thread(target=article_analysis_worker, args=(False, analysis_path))
    analysis_thread.daemon = True
    analysis_thread.start()
    return jsonify({'status': 'success', 'message': _t('Análise de Artigos iniciada.')})

@app.route('/api/reset_article_analysis', methods=['POST'])
@login_required
def reset_article_analysis():
    global analysis_thread
    
    data = request.get_json() or {}
    analysis_path = data.get('path', '').strip()
    
    if not analysis_path or not os.path.exists(analysis_path):
        return jsonify({'status': 'danger', 'message': f"{_t('A pasta')} '{analysis_path}' {_t('não está acessível.')}"})

    config = load_config()
    if config.get('article_analysis_path') != analysis_path:
        config['article_analysis_path'] = analysis_path
        save_config(config)

    if analysis_status['running']:
        return jsonify({'status': 'warning', 'message': _t('Pare a análise atual primeiro.')})
    
    analysis_thread = threading.Thread(target=article_analysis_worker, args=(True, analysis_path))
    analysis_thread.daemon = True
    analysis_thread.start()
    return jsonify({'status': 'success', 'message': _t('Análise de Artigos reiniciada.')})

@app.route('/api/stop_article_analysis', methods=['POST'])
@login_required
def stop_article_analysis():
    if not analysis_status['running']:
        return jsonify({'status': 'warning', 'message': _t('Nenhuma análise em curso.')})
    analysis_status['stop_flag'] = True
    return jsonify({'status': 'success', 'message': _t('A parar análise de artigos...')})

@app.route('/api/status_article_analysis')
@login_required
def status_article_analysis():
    return jsonify(analysis_status)

def get_shift_folder_path(linha, maquina, dst_path):
    today = datetime.now()
    date_folder = today.strftime("%Y-%m-%d")
    current_shift = get_current_shift()
    shift_path = os.path.join(dst_path, date_folder, current_shift)
    return shift_path

def save_config(config):
    if safe_save_json(CONFIG_FILE, config):
        logging.info("Configuração salva com sucesso")
        return True
    return False

def reset_counters():
    global files_copied_shift, files_copied_day, last_shift_reset, last_day_reset, counters_lock
    current_shift = get_current_shift()
    current_day = datetime.now().day
    with counters_lock:
        if current_shift != last_shift_reset:
            logging.info(f"Detectada mudança de turno. Novo turno: {current_shift}")
            files_copied_shift = {}
            last_shift_reset = current_shift
        if current_day != last_day_reset:
            logging.info(f"Detectada mudança de dia. Resetando contadores diários.")
            files_copied_day = {}
            last_day_reset = current_day

def copy_files_for_line(linha, maquina):
    global stop_copy_flags, active_folders, files_copied_shift, files_copied_day, counters_lock, last_seen_article_state
    thread_key = f"{linha}_{maquina}"
    logging.info(f"[{linha}/{maquina}] Thread de cópia iniciada.")
    
    last_src = None
    last_dst = None
            
    while not stop_copy_flags.get(thread_key, False):
        try:
            config = load_config()
            try:
                scan_interval = int(config.get('scan_interval_sec', 1))
            except Exception:
                scan_interval = 1
                
            linha_cfg = config.get('linhas', {}).get(linha, {})
            m_cfg = linha_cfg.get(maquina, {})
            
            src_path = m_cfg.get('src', '')
            dst_path = m_cfg.get('dst', '')
            delete_source_file = m_cfg.get('delete_source', True)

            if not src_path or not dst_path:
                time.sleep(scan_interval)
                continue

            src_path_norm = os.path.normpath(src_path)
            
            if src_path_norm != last_src or dst_path != last_dst:
                logging.info(f"[{linha}/{maquina}] Caminhos Ativos: ORIGEM='{src_path_norm}' | DESTINO='{dst_path}'")
                last_src = src_path_norm
                last_dst = dst_path

            if not os.path.exists(src_path_norm):
                time.sleep(scan_interval)
                continue

            reset_counters()
            current_shift_path = get_shift_folder_path(linha, maquina, dst_path)
            
            with counters_lock:
                if active_folders.get(thread_key) != current_shift_path:
                    active_folders[thread_key] = current_shift_path
                    os.makedirs(active_folders[thread_key], exist_ok=True)
            
            final_dst_path = active_folders[thread_key]

            ficheiros = []
            try:
                with os.scandir(src_path_norm) as entries:
                    for entry in entries:
                        ficheiros.append(entry.name)
            except OSError:
                time.sleep(scan_interval)
                continue
                
            extensoes_validas = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.xml'}
            
            for filename in ficheiros:
                if stop_copy_flags.get(thread_key, False): 
                    break
                    
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext not in extensoes_validas:
                    continue
                    
                file_path = os.path.join(src_path_norm, filename)
                final_dst_file = os.path.join(final_dst_path, filename)
                
                if not os.path.exists(final_dst_file):
                    try:
                        if not os.path.exists(file_path):
                            continue
                            
                        shutil.copy2(file_path, final_dst_file)
                        
                        if file_ext == '.xml':
                            try:
                                with open(final_dst_file, 'r', encoding='utf-8', errors='ignore') as f:
                                    chunk = f.read(2048)
                                    m_art = REGEX_NOM_ART.search(chunk)
                                    if m_art:
                                        art_name = m_art.group(1).strip()
                                        current_date_str = datetime.now().strftime("%Y-%m-%d")
                                        state_key = f"{linha}_{maquina}"
                                        
                                        needs_update = False
                                        with article_db_lock:
                                            current_state = last_seen_article_state.get(state_key)
                                            if not current_state or current_state['art'] != art_name or current_state['date'] != current_date_str:
                                                last_seen_article_state[state_key] = {'art': art_name, 'date': current_date_str}
                                                needs_update = True
                                                
                                        if needs_update:
                                            register_article_appearance(linha, current_date_str, art_name)
                            except Exception:
                                pass
                        
                        with counters_lock:
                            if thread_key not in files_copied_shift: files_copied_shift[thread_key] = {'jpg': 0, 'xml': 0}
                            if thread_key not in files_copied_day: files_copied_day[thread_key] = {'jpg': 0, 'xml': 0}
                            
                            if file_ext in ['.jpg', '.jpeg', '.png', '.bmp', '.tiff']:
                                files_copied_shift[thread_key]['jpg'] += 1
                                files_copied_day[thread_key]['jpg'] += 1
                            elif file_ext == '.xml':
                                files_copied_shift[thread_key]['xml'] += 1
                                files_copied_day[thread_key]['xml'] += 1
                                
                    except FileNotFoundError:
                        pass 
                    except PermissionError:
                        pass 
                    except Exception as e:
                        if "WinError 2" not in str(e) and "WinError 3" not in str(e):
                            logging.error(f"[{linha}/{maquina}] Erro ao copiar {filename}: {e}")
                
                if delete_source_file and os.path.exists(final_dst_file):
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except Exception:
                        pass
                        
            time.sleep(scan_interval)
            
        except Exception as e:
            logging.error(f"[{linha}/{maquina}] Erro geral na thread: {e}")
            time.sleep(5)
    
    logging.info(f"[{linha}/{maquina}] Thread de cópia finalizada.")

def mirror_ssd_service():
    global stop_mirror_flag
    logging.info("[MirrorSSD] Iniciando serviço de mirror SSD")
    
    try:
        config = load_config()
        source_path = config.get('mirror_source_path', '/volume1/inspecao_organizadas')
        ssd_path = config.get('ssd_path', '/volume2/ssd_mirror')
        retention_days = config.get('ssd_retention_days', 5)
        scan_interval = config.get('scan_interval_sec', 1)
        
        if not os.path.exists(source_path):
            logging.error(f"[MirrorSSD] Diretório origem não existe: {source_path}")
            return
            
        os.makedirs(ssd_path, exist_ok=True)
        
        while not stop_mirror_flag:
            try:
                cmd = ['rsync', '-a', '--delete', f"{source_path}/", ssd_path]
                subprocess.run(cmd, check=True, capture_output=True, text=True)

                if stop_mirror_flag: continue

                if retention_days > 0:
                    cutoff_date = datetime.now() - timedelta(days=retention_days)
                    for item_name in os.listdir(ssd_path):
                        full_path = os.path.join(ssd_path, item_name)
                        if not os.path.isdir(full_path): continue
                        try:
                            folder_date = datetime.strptime(item_name, "%Y-%m-%d")
                            if folder_date < cutoff_date:
                                shutil.rmtree(full_path)
                        except ValueError: continue
                        except Exception: continue
                
                time.sleep(scan_interval * 2)
            except Exception:
                time.sleep(scan_interval)
                
    except Exception as e:
        logging.error(f"[MirrorSSD] Erro fatal: {e}")
    
    logging.info("[MirrorSSD] Serviço parado")

def pkiris_backup_service():
    global stop_pkiris_flag
    logging.info("[PKIRIS] Iniciando serviço de backup PKIRIS em background.")
    
    while not stop_pkiris_flag:
        try:
            config = load_config()
            dst_root = config.get('pkiris_dst_root', '')
            try:
                retention_days = int(config.get('pkiris_retention_days', 5))
            except:
                retention_days = 5
                
            if not dst_root:
                time.sleep(60)
                continue

            for linha, linha_cfg in config.get('linhas', {}).items():
                if stop_pkiris_flag: break
                for maquina, m_cfg in linha_cfg.items():
                    if not isinstance(m_cfg, dict): continue
                    
                    src_path = m_cfg.get('pkiris_src', '')
                    if not src_path or not os.path.exists(src_path):
                        continue
                    
                    safe_maq = maquina.replace(" ", "_").capitalize()
                    machine_dst = os.path.join(dst_root, f"Linha_{linha}", safe_maq)
                    os.makedirs(machine_dst, exist_ok=True)

                    try:
                        with os.scandir(src_path) as entries:
                            for entry in entries:
                                if stop_pkiris_flag: break
                                if entry.is_file() and entry.name.lower().endswith('.pkiris'):
                                    dst_file = os.path.join(machine_dst, entry.name)
                                    src_mtime = entry.stat().st_mtime
                                    
                                    needs_copy = True
                                    if os.path.exists(dst_file):
                                        dst_mtime = os.path.getmtime(dst_file)
                                        if src_mtime <= dst_mtime + 2: 
                                            needs_copy = False
                                            
                                    if needs_copy:
                                        shutil.copy2(entry.path, dst_file)
                                        logging.info(f"[PKIRIS] Copiado novo backup: {entry.name} -> {machine_dst}")
                                        
                        cutoff_time = time.time() - (retention_days * 86400)
                        with os.scandir(machine_dst) as entries:
                            for entry in entries:
                                if entry.is_file() and entry.name.lower().endswith('.pkiris'):
                                    if entry.stat().st_mtime < cutoff_time:
                                        try:
                                            os.remove(entry.path)
                                            logging.info(f"[PKIRIS] Retenção aplicada, apagado: {entry.name}")
                                        except Exception as e:
                                            logging.error(f"[PKIRIS] Erro ao remover {entry.name}: {e}")

                    except Exception as e:
                        logging.error(f"[PKIRIS] Erro ao processar maquina {linha}/{maquina}: {e}")

            for _ in range(60):
                if stop_pkiris_flag: break
                time.sleep(1)
                
        except Exception as e:
            logging.error(f"[PKIRIS] Erro crítico no serviço: {e}")
            time.sleep(60)
            
    logging.info("[PKIRIS] Serviço parado.")

def start_pkiris_service():
    global pkiris_thread, stop_pkiris_flag
    if pkiris_thread and pkiris_thread.is_alive(): return True
    stop_pkiris_flag = False
    pkiris_thread = threading.Thread(target=pkiris_backup_service)
    pkiris_thread.daemon = True
    pkiris_thread.start()
    return True

def stop_pkiris_service():
    global pkiris_thread, stop_pkiris_flag
    stop_pkiris_flag = True
    if pkiris_thread and pkiris_thread.is_alive():
        pkiris_thread.join(timeout=5)

def cleanup_retention_tree(dst_root, retention_days, service_name):
    if retention_days <= 0: return
    cutoff = datetime.now() - timedelta(days=retention_days)
    try:
        if not os.path.exists(dst_root): return
        for linha_dir in os.listdir(dst_root):
            linha_path = os.path.join(dst_root, linha_dir)
            if not os.path.isdir(linha_path): continue
            for maq_dir in os.listdir(linha_path):
                maq_path = os.path.join(linha_path, maq_dir)
                if not os.path.isdir(maq_path): continue
                for month_dir in os.listdir(maq_path):
                    month_path = os.path.join(maq_path, month_dir)
                    if not os.path.isdir(month_path): continue
                    for day_dir in os.listdir(month_path):
                        day_path = os.path.join(month_path, day_dir)
                        if not os.path.isdir(day_path): continue
                        try:
                            folder_date = datetime.strptime(f"{month_dir}-{day_dir}", "%Y-%m-%d")
                            if folder_date < cutoff:
                                shutil.rmtree(day_path)
                                logging.info(f"[{service_name}] Retenção ({retention_days} dias) aplicada. Apagado: {day_path}")
                        except ValueError:
                            pass
    except Exception as e:
        logging.error(f"[{service_name}] Erro ao limpar retenção: {e}")

def historicos_backup_service():
    global stop_historicos_flag
    logging.info("[HISTORICOS] Iniciando serviço de backup de Históricos em background.")
    last_run_hour = -1
    
    while not stop_historicos_flag:
        try:
            now = datetime.now()
            # Corre aos 55 minutos e certifica-se de que corre apenas uma vez nessa hora
            if now.minute == 55 and now.hour != last_run_hour:
                last_run_hour = now.hour
                config = load_config()
                dst_root = config.get('historicos_dst_root', '')
                try:
                    retention_days = int(config.get('historicos_retention_days', 365))
                except:
                    retention_days = 365
                
                if dst_root:
                    for linha, linha_cfg in config.get('linhas', {}).items():
                        if stop_historicos_flag: break
                        for maquina, m_cfg in linha_cfg.items():
                            if not isinstance(m_cfg, dict): continue
                            src_path = m_cfg.get('historico_src', '')
                            if not src_path or not os.path.exists(src_path): continue
                            
                            month_str = now.strftime("%Y-%m")
                            day_str = now.strftime("%d")
                            safe_maq = maquina.replace(" ", "_").capitalize()
                            machine_dst = os.path.join(dst_root, f"Linha_{linha}", safe_maq, month_str, day_str)
                            os.makedirs(machine_dst, exist_ok=True)
                            
                            try:
                                with os.scandir(src_path) as entries:
                                    for entry in entries:
                                        if stop_historicos_flag: break
                                        if entry.is_file():
                                            file_mtime = datetime.fromtimestamp(entry.stat().st_mtime)
                                            # Copia apenas os ficheiros modificados no dia corrente
                                            if file_mtime.date() == now.date():
                                                dst_file = os.path.join(machine_dst, entry.name)
                                                if not os.path.exists(dst_file):
                                                    shutil.copy2(entry.path, dst_file)
                                                    logging.info(f"[HISTORICOS] Copiado: {entry.name} -> {machine_dst}")
                            except Exception as e:
                                logging.error(f"[HISTORICOS] Erro ao ler pasta {src_path}: {e}")
                                
                    cleanup_retention_tree(dst_root, retention_days, "HISTORICOS")
                    
        except Exception as e:
            logging.error(f"[HISTORICOS] Erro crítico no serviço: {e}")
            
        for _ in range(30):
            if stop_historicos_flag: break
            time.sleep(1)

    logging.info("[HISTORICOS] Serviço parado.")

def artigos_backup_service():
    global stop_artigos_flag
    logging.info("[ARTIGOS] Iniciando serviço de backup de Artigos em background.")
    
    while not stop_artigos_flag:
        try:
            now = datetime.now()
            config = load_config()
            dst_root = config.get('artigos_dst_root', '')
            try:
                retention_days = int(config.get('artigos_retention_days', 365))
            except:
                retention_days = 365
                
            if dst_root:
                for linha, linha_cfg in config.get('linhas', {}).items():
                    if stop_artigos_flag: break
                    for maquina, m_cfg in linha_cfg.items():
                        if not isinstance(m_cfg, dict): continue
                        src_path = m_cfg.get('artigo_src', '')
                        if not src_path or not os.path.exists(src_path): continue
                        
                        month_str = now.strftime("%Y-%m")
                        day_str = now.strftime("%d")
                        safe_maq = maquina.replace(" ", "_").capitalize()
                        machine_dst = os.path.join(dst_root, f"Linha_{linha}", safe_maq, month_str, day_str)
                        os.makedirs(machine_dst, exist_ok=True)
                        
                        try:
                            with os.scandir(src_path) as entries:
                                for entry in entries:
                                    if stop_artigos_flag: break
                                    if entry.is_file():
                                        file_mtime = datetime.fromtimestamp(entry.stat().st_mtime)
                                        # Identifica o artigo corrente gravado no próprio dia
                                        if file_mtime.date() == now.date():
                                            dst_file = os.path.join(machine_dst, entry.name)
                                            if not os.path.exists(dst_file):
                                                shutil.copy2(entry.path, dst_file)
                                                logging.info(f"[ARTIGOS] Copiado artigo corrente: {entry.name} -> {machine_dst}")
                        except Exception as e:
                            logging.error(f"[ARTIGOS] Erro ao ler pasta {src_path}: {e}")
                            
                cleanup_retention_tree(dst_root, retention_days, "ARTIGOS")
                
        except Exception as e:
            logging.error(f"[ARTIGOS] Erro crítico no serviço: {e}")
            
        # Repousa 1 hora antes de verificar os artigos novamente
        for _ in range(3600):
            if stop_artigos_flag: break
            time.sleep(1)

    logging.info("[ARTIGOS] Serviço parado.")

def start_historicos_service():
    global historicos_thread, stop_historicos_flag
    if historicos_thread and historicos_thread.is_alive(): return True
    stop_historicos_flag = False
    historicos_thread = threading.Thread(target=historicos_backup_service)
    historicos_thread.daemon = True
    historicos_thread.start()
    return True

def stop_historicos_service():
    global historicos_thread, stop_historicos_flag
    stop_historicos_flag = True
    if historicos_thread and historicos_thread.is_alive():
        historicos_thread.join(timeout=5)

def start_artigos_service():
    global artigos_thread, stop_artigos_flag
    if artigos_thread and artigos_thread.is_alive(): return True
    stop_artigos_flag = False
    artigos_thread = threading.Thread(target=artigos_backup_service)
    artigos_thread.daemon = True
    artigos_thread.start()
    return True

def stop_artigos_service():
    global artigos_thread, stop_artigos_flag
    stop_artigos_flag = True
    if artigos_thread and artigos_thread.is_alive():
        artigos_thread.join(timeout=5)

def start_file_copying_service():
    global copy_threads, stop_copy_flags
    config = load_config()
    active_count = 0
    for linha, linha_config in config.get('linhas', {}).items():
        for maquina, maquina_config in linha_config.items():
            if not isinstance(maquina_config, dict): continue
            key = f"{linha}_{maquina}"
            if not maquina_config.get('backup_active', False): continue
            if key not in copy_threads or not copy_threads[key].is_alive():
                stop_copy_flags[key] = False
                copy_threads[key] = threading.Thread(target=copy_files_for_line, args=(linha, maquina))
                copy_threads[key].daemon = True
                copy_threads[key].start()
                active_count += 1
    return active_count

def stop_file_copying_service():
    global copy_threads, stop_copy_flags
    for key in list(copy_threads.keys()):
        stop_copy_flags[key] = True
        if copy_threads[key].is_alive():
            copy_threads[key].join(timeout=5)
    copy_threads.clear()
    stop_copy_flags.clear()

def start_mirror_ssd_service():
    global mirror_thread, stop_mirror_flag
    if mirror_thread and mirror_thread.is_alive(): return True
    stop_mirror_flag = False
    mirror_thread = threading.Thread(target=mirror_ssd_service)
    mirror_thread.daemon = True
    mirror_thread.start()
    return True

def stop_mirror_ssd_service():
    global mirror_thread, stop_mirror_flag
    stop_mirror_flag = True
    if mirror_thread and mirror_thread.is_alive():
        mirror_thread.join(timeout=10)

def start_mosaic_process(linha, maquina):
    with mosaic_lock:
        key = f"{linha}_{maquina}"
        if key in mosaic_processes and mosaic_processes[key].poll() is None: return True

        config = load_config()
        if linha == 'Global':
            if maquina == 'lateral': port = config.get('visao_global', {}).get('port_lateral', 5098)
            elif maquina == 'fundo': port = config.get('visao_global', {}).get('port_fundo', 5099)
            else: port = config.get('visao_global', {}).get('mosaic_port', 5099) 
        else:
            port = config.get('linhas', {}).get(linha, {}).get(maquina, {}).get('mosaic_port')

        if not port or port == 0: return False
        
        mosaic_script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mosaic_complete.py')
        if not os.path.exists(mosaic_script_path): return False

        command = [sys.executable, mosaic_script_path, str(port), linha, maquina]
        try:
            process = subprocess.Popen(command)
            mosaic_processes[key] = process
            return True
        except Exception: return False

def stop_mosaic_process(linha, maquina):
    with mosaic_lock:
        key = f"{linha}_{maquina}"
        if key in mosaic_processes:
            process = mosaic_processes[key]
            if process.poll() is None:
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True): child.terminate()
                    parent.terminate()
                    parent.wait(timeout=5)
                except: process.kill()
            del mosaic_processes[key]
            return True
        return False

def start_all_active_mosaics():
    config = load_config()
    for linha, linha_config in config.get('linhas', {}).items():
        for maquina, maquina_config in linha_config.items():
            if isinstance(maquina_config, dict) and maquina_config.get('mosaic_active', False):
                start_mosaic_process(linha, maquina)
                
    vg_config = config.get('visao_global', {})
    if vg_config.get('mosaic_lateral_active', False): start_mosaic_process('Global', 'lateral')
    if vg_config.get('mosaic_fundo_active', False): start_mosaic_process('Global', 'fundo')

def stop_all_mosaic_processes():
    with mosaic_lock: keys_to_stop = list(mosaic_processes.keys())
    for key in keys_to_stop:
        linha, maquina = key.split('_', 1)
        stop_mosaic_process(linha, maquina)

atexit.register(stop_all_mosaic_processes)

def start_public_portal():
    global public_portal_process
    if public_portal_process and public_portal_process.poll() is None:
        return True
    
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public_history_portal.py')
    if not os.path.exists(script_path):
        logging.error("Ficheiro public_history_portal.py não encontrado!")
        return False
        
    command = [sys.executable, script_path, "5581"]
    try:
        public_portal_process = subprocess.Popen(command)
        logging.info("Portal Público Iniciado no processo PID: " + str(public_portal_process.pid))
        return True
    except Exception as e:
        logging.error(f"Erro ao iniciar portal público: {e}")
        return False

def stop_public_portal():
    global public_portal_process
    if public_portal_process:
        if public_portal_process.poll() is None:
            try:
                parent = psutil.Process(public_portal_process.pid)
                for child in parent.children(recursive=True): child.terminate()
                parent.terminate()
                parent.wait(timeout=5)
            except: public_portal_process.kill()
        public_portal_process = None

atexit.register(stop_public_portal)

def start_pen_pkiris_portal():
    global pen_pkiris_process
    if pen_pkiris_process and pen_pkiris_process.poll() is None:
        return True
    
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'criacao_pen_pkiris.py')
    if not os.path.exists(script_path):
        logging.error("Ficheiro criacao_pen_pkiris.py não encontrado!")
        return False
        
    command = [sys.executable, script_path]
    try:
        pen_pkiris_process = subprocess.Popen(command)
        logging.info("Portal Pen PKIRIS Iniciado no processo PID: " + str(pen_pkiris_process.pid))
        return True
    except Exception as e:
        logging.error(f"Erro ao iniciar portal Pen PKIRIS: {e}")
        return False

def stop_pen_pkiris_portal():
    global pen_pkiris_process
    if pen_pkiris_process:
        if pen_pkiris_process.poll() is None:
            try:
                parent = psutil.Process(pen_pkiris_process.pid)
                for child in parent.children(recursive=True): child.terminate()
                parent.terminate()
                parent.wait(timeout=5)
            except: pen_pkiris_process.kill()
        pen_pkiris_process = None

atexit.register(stop_pen_pkiris_portal)

def check_path_accessible(path):
    try:
        path = os.path.normpath(path.strip())
        if not path: return False, "Caminho vazio"
        if os.path.exists(path):
            if os.access(path, os.R_OK): return True, None
            return False, f"Sem permissão de leitura"
        return False, f"Caminho não existe"
    except Exception as e: return False, f"Erro: {str(e)}"

def browse_directory(path):
    try:
        if not path: path = '/'
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent == path: break
            path = parent
        if not os.path.isdir(path):
            path = os.path.abspath(os.sep) if platform.system() == 'Windows' else '/'
        path = path.replace('\\', '/')
        if path == '': path = '/'
        
        directories = []
        files = []
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir():
                    directories.append(entry.name)
                else:
                    files.append(entry.name)
        
        directories.sort()
        files.sort()
        return {'success': True, 'current_path': path, 'contents': {'directories': directories, 'files': files[:50]}}
    except Exception as e: return {'success': False, 'error': str(e)}

def get_disk_usage(path):
    try:
        if os.path.exists(path):
            usage = shutil.disk_usage(path)
            return {'percent': round((usage.used / usage.total) * 100, 1)}
    except: pass
    return {'percent': 0}

def get_connected_ips():
    try:
        config = load_config()
        port_map = {
            5580: _t("Painel de Gestão"),
            5581: _t("Portal de Histórico"),
            5582: _t("Portal Pen PKIRIS"),
        }
        
        vg_cfg = config.get('visao_global', {})
        if vg_cfg.get('port_lateral'): port_map[int(vg_cfg['port_lateral'])] = _t("Visão Global - Laterais")
        if vg_cfg.get('port_fundo'): port_map[int(vg_cfg['port_fundo'])] = _t("Visão Global - Fundos")
        
        for linha, l_cfg in config.get('linhas', {}).items():
            for maq, m_cfg in l_cfg.items():
                if isinstance(m_cfg, dict) and m_cfg.get('mosaic_port'):
                    port_map[int(m_cfg['mosaic_port'])] = f"{_t('Mosaico')} {linha} - {_t(maq.capitalize())}"
                    
        ips = []
        seen_ips = set()
        connections = psutil.net_connections(kind='inet')
        for conn in connections:
            if conn.status == 'ESTABLISHED' and conn.raddr and conn.laddr:
                ip = conn.raddr.ip
                if ip in ['127.0.0.1', '::1', 'localhost'] or ip in seen_ips:
                    continue
                local_port = conn.laddr.port
                
                page_name = port_map.get(local_port, f"{_t('Porta')} {local_port}")
                
                ips.append({
                    'ip': ip, 
                    'port': conn.raddr.port, 
                    'local_port': local_port,
                    'page': page_name,
                    'status': 'online'
                })
                seen_ips.add(ip)
        return ips[:30]
    except Exception as e:
        logging.error(f"Erro ao obter IPs: {e}")
        return []

def build_export_zip_task(task_id, export_date, selected_turnos, selected_machines, compress):
    try:
        config = load_config()
        linhas_config = config.get('linhas', {})
        files_to_zip = []
        for machine_key in selected_machines:
            if '_' not in machine_key: continue
            linha_num, maquina_nome = machine_key.split('_', 1)
            dst_path = linhas_config.get(linha_num, {}).get(maquina_nome, {}).get('dst')
            if not dst_path: continue
            for turno_num in selected_turnos:
                folder_path = os.path.join(dst_path, export_date, f"turno{turno_num}")
                if os.path.isdir(folder_path):
                    with os.scandir(folder_path) as it:
                        for entry in it:
                            if entry.is_file() and entry.name.lower().endswith(('.jpg', '.xml')):
                                file_path = entry.path
                                arcname = os.path.join(f"Linha_{linha_num}", maquina_nome, f"turno{turno_num}", entry.name)
                                files_to_zip.append((file_path, arcname))
        total_files = len(files_to_zip)
        if total_files == 0:
            export_tasks[task_id] = {'status': 'error', 'message': 'Nenhum ficheiro encontrado para esta data/turno.', 'progress': 0}
            return
        temp_dir = tempfile.gettempdir()
        zip_filename = os.path.join(temp_dir, f"export_{task_id}.zip")
        comp_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        with zipfile.ZipFile(zip_filename, 'w', comp_type) as zf:
            for i, (file_path, arcname) in enumerate(files_to_zip):
                zf.write(file_path, arcname)
                if i % max(1, (total_files // 100)) == 0: export_tasks[task_id]['progress'] = int((i / total_files) * 100)
        export_tasks[task_id]['progress'] = 100
        export_tasks[task_id]['status'] = 'completed'
        export_tasks[task_id]['file'] = zip_filename
    except Exception as e:
        export_tasks[task_id] = {'status': 'error', 'message': str(e), 'progress': 0}

@app.route('/api/restart_terminal', methods=['POST'])
@login_required
def restart_terminal():
    data = request.get_json()
    ip = data.get('ip')
    if not ip:
        return jsonify({'status': 'error', 'message': _t('IP inválido.')})
    
    def reboot_task(target_ip):
        try:
            subprocess.run(['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=3', f'pi@{target_ip}', 'sudo reboot'], timeout=5)
        except Exception:
            pass
            
    threading.Thread(target=reboot_task, args=(ip,)).start()
    log_user_action(session.get('username'), f"Comando de reinício enviado para o IP {ip}")
    
    return jsonify({'status': 'success', 'message': f"{_t('Comando de reinício enviado para')} {ip}."})

@app.route('/api/export_start', methods=['POST'])
@login_required
def export_start():
    data = request.get_json()
    task_id = uuid.uuid4().hex 
    export_tasks[task_id] = {'status': 'processing', 'progress': 0, 'file': None, 'message': ''}
    t = threading.Thread(target=build_export_zip_task, args=(task_id, data['export_date'], data['turnos'], data['machines'], data.get('compress', True)))
    t.daemon = True
    t.start()
    return jsonify({'task_id': task_id})

@app.route('/api/export_status/<task_id>')
@login_required
def export_status(task_id):
    return jsonify(export_tasks.get(task_id, {'status': 'error', 'message': _t('Task não encontrada.')}))

@app.route('/api/export_download/<task_id>')
@login_required
def export_download(task_id):
    task = export_tasks.get(task_id)
    if not task or task['status'] != 'completed' or not task['file']: return _t("Ficheiro não encontrado"), 404
    return send_file(task['file'], as_attachment=True, download_name='export.zip')

@app.route('/historico_externo')
def redirect_to_public_portal():
    return render_template_string("<script>window.location.href = 'http://' + window.location.hostname + ':5581/';</script>")

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        users = load_users()
        user_data = users.get(username)
        if user_data and check_password_hash(user_data['password'], password):
            session['username'] = username
            session['is_dev'] = user_data.get('is_dev', False)
            log_user_action(username, "Logged in successfully")
            return redirect(url_for('index'))
        else:
            log_user_action(username, "Failed login attempt")
            error = 'Utilizador ou password inválida.'
    logo_data = ""
    try:
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logo_Green_Letters_white.png')
        with open(logo_path, "rb") as image_file:
            logo_data = base64.b64encode(image_file.read()).decode('utf-8')
    except Exception: pass
    return render_template_string(LOGIN_TEMPLATE, error=error, logo_data=logo_data)

@app.route('/logout')
def logout():
    log_user_action(session.get('username', 'Unknown'), "Logged out")
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(BACKUP_TEMPLATE, config=load_config())

@app.route('/get_config')
@login_required
def get_config_api():
    return jsonify(load_config())

@app.route('/get_line_status')
@login_required
def get_line_status():
    config = load_config()
    lines_list, backup_services, mosaic_services = [], {}, {}
    for linha, l_config in config.get('linhas', {}).items():
        for maquina, m_config in l_config.items():
            if not isinstance(m_config, dict): continue
            key = f"{linha}_{maquina}"
            lines_list.append({'linha': linha, 'maquina': maquina, 'maquina_display': _t(maquina.capitalize()), 'key': key})
            backup_services[key] = m_config.get('backup_active', False) and (key in copy_threads and copy_threads[key].is_alive())
            mosaic_services[key] = m_config.get('mosaic_active', False) and (key in mosaic_processes and mosaic_processes[key].poll() is None)
    return jsonify({'lines': lines_list, 'backup_services': backup_services, 'mosaic_services': mosaic_services, 'shift_counters': files_copied_shift, 'day_counters': files_copied_day})

@app.route('/browse_directory', methods=['POST'])
@login_required
def api_browse_directory():
    path = request.get_json().get('path', '/')
    return jsonify(browse_directory(path))

@app.route('/check_path_access', methods=['POST'])
@login_required
def api_check_path_access():
    path = request.get_json().get('path', '')
    accessible, error = check_path_accessible(path)
    return jsonify({'accessible': accessible, 'error': error})

@app.route('/service_status')
@login_required
def api_service_status():
    config = load_config()
    backup_services, mosaic_process_status = {}, {}
    for linha, l_config in config.get('linhas', {}).items():
        for maquina, m_config in l_config.items():
            if not isinstance(m_config, dict): continue
            key = f"{linha}_{maquina}"
            backup_services[key] = m_config.get('backup_active', False)
            mosaic_process_status[key] = {'running': key in mosaic_processes and mosaic_processes[key].poll() is None}
    
    mosaic_process_status['Global_lateral'] = {'running': 'Global_lateral' in mosaic_processes and mosaic_processes['Global_lateral'].poll() is None}
    mosaic_process_status['Global_fundo'] = {'running': 'Global_fundo' in mosaic_processes and mosaic_processes['Global_fundo'].poll() is None}
    
    return jsonify({
        'backup_services': backup_services, 
        'mosaic_process_status': mosaic_process_status, 
        'file_copying_active': any(t.is_alive() for t in copy_threads.values()), 
        'mirror_ssd_active': mirror_thread and mirror_thread.is_alive(),
        'pkiris_active': pkiris_thread and pkiris_thread.is_alive(),
        'historicos_active': historicos_thread and historicos_thread.is_alive(),
        'artigos_active': artigos_thread and artigos_thread.is_alive(),
        'pen_pkiris_active': pen_pkiris_process and pen_pkiris_process.poll() is None
    })

@app.route('/api/users', methods=['GET'])
@login_required
def get_users():
    users = load_users()
    user_list = [{"username": u, "is_dev": d.get("is_dev", False)} for u, d in users.items()]
    return jsonify({"users": user_list})

@app.route('/api/users/create', methods=['POST'])
@login_required
def create_user():
    if not session.get('is_dev'): return jsonify({"status": "danger", "message": _t("Apenas developers.")}), 403
    data = request.json
    username, password = data.get('username'), data.get('password')
    if not username or not password: return jsonify({"status": "danger", "message": _t("Faltam dados.")})
    users = load_users()
    if username in users: return jsonify({"status": "danger", "message": _t("Já existe.")})
    users[username] = {"password": generate_password_hash(password), "is_dev": False}
    save_users(users)
    log_user_action(session['username'], f"Created new user: {username}")
    return jsonify({"status": "success", "message": _t("Utilizador criado.")})

@app.route('/api/users/change_password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    current_password, new_password = data.get('current_password'), data.get('new_password')
    username = session['username']
    users = load_users()
    if not session.get('is_dev'):
        if not check_password_hash(users[username]['password'], current_password): return jsonify({"status": "danger", "message": _t("Password atual incorreta.")})
    users[username]['password'] = generate_password_hash(new_password)
    save_users(users)
    log_user_action(username, "Changed their own password")
    return jsonify({"status": "success", "message": _t("Password alterada.")})

@app.route('/api/users/delete', methods=['POST'])
@login_required
def delete_user():
    if not session.get('is_dev'): return jsonify({"status": "danger", "message": _t("Sem permissão.")}), 403
    username_to_delete = request.json.get('username')
    if username_to_delete == 'cid': return jsonify({"status": "danger", "message": _t("Não permitido.")})
    users = load_users()
    if username_to_delete in users:
        del users[username_to_delete]
        save_users(users)
        log_user_action(session['username'], f"Deleted user: {username_to_delete}")
        return jsonify({"status": "success", "message": _t("Apagado.")})
    return jsonify({"status": "danger", "message": _t("Não encontrado.")})

@app.route('/start_file_copying', methods=['POST'])
@login_required
def start_file_copying_route():
    count = start_file_copying_service()
    if count > 0: return jsonify({'status': 'success', 'message': f"{_t('Iniciado com')} {count} {_t('máquinas.')}"})
    return jsonify({'status': 'error', 'message': _t('Nenhuma ativa.')})

@app.route('/start_mirror_ssd', methods=['POST'])
@login_required
def start_mirror_ssd_route():
    if start_mirror_ssd_service(): return jsonify({'status': 'success', 'message': _t('Iniciado.')})
    return jsonify({'status': 'error', 'message': _t('Erro.')})

@app.route('/start_pkiris', methods=['POST'])
@login_required
def start_pkiris_route():
    if start_pkiris_service(): return jsonify({'status': 'success', 'message': _t('Backup PKIRIS iniciado.')})
    return jsonify({'status': 'error', 'message': _t('Erro.')})

@app.route('/stop_pkiris', methods=['POST'])
@login_required
def stop_pkiris_route():
    stop_pkiris_service()
    return jsonify({'status': 'success', 'message': _t('Backup PKIRIS parado.')})

@app.route('/start_historicos', methods=['POST'])
@login_required
def start_historicos_route():
    if start_historicos_service(): return jsonify({'status': 'success', 'message': _t('Backup Históricos iniciado.')})
    return jsonify({'status': 'error', 'message': _t('Erro.')})

@app.route('/stop_historicos', methods=['POST'])
@login_required
def stop_historicos_route():
    stop_historicos_service()
    return jsonify({'status': 'success', 'message': _t('Backup Históricos parado.')})

@app.route('/start_artigos', methods=['POST'])
@login_required
def start_artigos_route():
    if start_artigos_service(): return jsonify({'status': 'success', 'message': _t('Backup Artigos iniciado.')})
    return jsonify({'status': 'error', 'message': _t('Erro.')})

@app.route('/stop_artigos', methods=['POST'])
@login_required
def stop_artigos_route():
    stop_artigos_service()
    return jsonify({'status': 'success', 'message': _t('Backup Artigos parado.')})

@app.route('/stop_all_services', methods=['POST'])
@login_required
def api_stop_all_services():
    stop_file_copying_service()
    stop_mirror_ssd_service()
    stop_all_mosaic_processes()
    stop_pkiris_service()
    stop_historicos_service()
    stop_artigos_service()
    stop_public_portal()
    stop_pen_pkiris_portal()
    return jsonify({'status': 'success', 'message': _t('Todos os serviços parados.')})

@app.route('/copy_status')
@login_required
def api_copy_status():
    active_threads = sum(1 for t in copy_threads.values() if t.is_alive())
    return jsonify({'running': active_threads > 0, 'active_threads': active_threads})

@app.route('/list_directories')
@login_required
def list_directories():
    base_path = '/volume1/inspecao_organizadas'
    dirs = [d.name for d in os.scandir(base_path) if d.is_dir()] if os.path.exists(base_path) else []
    return jsonify({'directories': dirs})

@app.route('/connected_ips')
@login_required
def api_connected_ips():
    return jsonify({'ips': get_connected_ips()})

@app.route('/get_machines_list')
@login_required
def get_machines_list():
    config = load_config()
    machines = []
    for linha, l_config in config.get('linhas', {}).items():
        for maquina in l_config:
            if isinstance(l_config[maquina], dict): machines.append({'key': f"{linha}_{maquina}", 'linha': linha, 'maquina': maquina, 'maquina_display': _t(maquina.capitalize())})
    return jsonify({'machines': machines})

@app.route('/save_general_config', methods=['POST'])
@login_required
def save_general_config_route():
    data, config = request.get_json(), load_config()
    config.update({
        "ssd_path": data.get("ssd_path"), 
        "mirror_source_path": data.get("mirror_source_path"),
        "mosaic_config_folder": data.get("mosaic_config_folder"),
        "article_analysis_path": data.get("article_analysis_path"),
        "log_file_path": data.get("log_file_path"),
        "mirror_include_subfolders": data.get("mirror_include_subfolders", True),
        "ssd_retention_days": int(data.get("ssd_retention_days", 5)),
        "hdd_retention_months": int(data.get("hdd_retention_months", 6)),
        "scan_interval_sec": int(data.get("scan_interval_sec", 1)),
        "mosaic_source_path": data.get("mosaic_source_path"), 
        "turnos": data.get("turnos")
    })
    if save_config(config):
        log_user_action(session['username'], "Saved general configurations")
        return jsonify({'status': 'success', 'message': _t('Configuração geral salva.')})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar.')})

@app.route('/save_lines_config', methods=['POST'])
@login_required
def save_lines_config_route():
    data = request.get_json()
    config = load_config()
    if 'linhas' not in config:
        config['linhas'] = {}
        
    if 'linhas' in data:
        for linha_key, submitted_line_data in data.get('linhas', {}).items():
            if linha_key not in config['linhas']:
                config['linhas'][linha_key] = {}
            
            if 'cycle_mode_active' in submitted_line_data: 
                config['linhas'][linha_key]['cycle_mode_active'] = submitted_line_data['cycle_mode_active']
            if 'cycle_time_sec' in submitted_line_data: 
                config['linhas'][linha_key]['cycle_time_sec'] = submitted_line_data['cycle_time_sec']
            if 'use_test_mode' in submitted_line_data:
                config['linhas'][linha_key]['use_test_mode'] = submitted_line_data['use_test_mode']
            
            use_test = config['linhas'][linha_key].get('use_test_mode', False)
            
            for maquina_key, submitted_machine_data in submitted_line_data.items():
                if isinstance(submitted_machine_data, dict): 
                    if maquina_key not in config['linhas'][linha_key]:
                        config['linhas'][linha_key][maquina_key] = {}
                    
                    config['linhas'][linha_key][maquina_key].update(submitted_machine_data)
                    
                    src_p = submitted_machine_data.get('src_prod', config['linhas'][linha_key][maquina_key].get('src_prod', ''))
                    dst_p = submitted_machine_data.get('dst_prod', config['linhas'][linha_key][maquina_key].get('dst_prod', ''))
                    src_t = submitted_machine_data.get('src_test', config['linhas'][linha_key][maquina_key].get('src_test', ''))
                    dst_t = submitted_machine_data.get('dst_test', config['linhas'][linha_key][maquina_key].get('dst_test', ''))
                    
                    if not src_p: src_p = config['linhas'][linha_key][maquina_key].get('src', '')
                    if not dst_p: dst_p = config['linhas'][linha_key][maquina_key].get('dst', '')
                    
                    if use_test:
                        config['linhas'][linha_key][maquina_key]['src'] = src_t if src_t else src_p
                        config['linhas'][linha_key][maquina_key]['dst'] = dst_t if dst_t else dst_p
                    else:
                        config['linhas'][linha_key][maquina_key]['src'] = src_p
                        config['linhas'][linha_key][maquina_key]['dst'] = dst_p
                    
                    config['linhas'][linha_key][maquina_key]['src_prod'] = src_p
                    config['linhas'][linha_key][maquina_key]['dst_prod'] = dst_p
                    
    if 'visao_global' in data:
        if 'visao_global' not in config: config['visao_global'] = {}
        config['visao_global']['port_lateral'] = data['visao_global'].get('port_lateral', 5098)
        config['visao_global']['port_fundo'] = data['visao_global'].get('port_fundo', 5099)
        config['visao_global']['cycle_mode_active'] = data['visao_global'].get('cycle_mode_active', True)
        config['visao_global']['cycle_time_sec'] = data['visao_global'].get('cycle_time_sec', 30)

    if save_config(config):
        log_user_action(session['username'], "Saved line configurations")
        return jsonify({'status': 'success', 'message': _t('Configurações salvas.')})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar.')})

@app.route('/save_pkiris_config', methods=['POST'])
@login_required
def save_pkiris_config():
    data = request.get_json()
    config = load_config()
    
    config['pkiris_retention_days'] = int(data.get('pkiris_retention_days', 5))
    config['pkiris_dst_root'] = data.get('pkiris_dst_root', '')
    
    if 'linhas' not in config:
        config['linhas'] = {}
        
    for linha, maquinas in data.get('linhas', {}).items():
        if linha not in config['linhas']:
            config['linhas'][linha] = {}
        for maquina, src in maquinas.items():
            if maquina not in config['linhas'][linha]:
                config['linhas'][linha][maquina] = {}
            config['linhas'][linha][maquina]['pkiris_src'] = src
            
    if save_config(config):
        log_user_action(session['username'], "Saved PKIRIS Backup configurations")
        return jsonify({'status': 'success', 'message': _t('Configurações PKIRIS salvas com sucesso.')})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar.')})

@app.route('/save_historicos_config', methods=['POST'])
@login_required
def save_historicos_config():
    data = request.get_json()
    config = load_config()
    
    config['historicos_retention_days'] = int(data.get('historicos_retention_days', 365))
    config['historicos_dst_root'] = data.get('historicos_dst_root', '')
    
    if 'linhas' not in config:
        config['linhas'] = {}
        
    for linha, maquinas in data.get('linhas', {}).items():
        if linha not in config['linhas']:
            config['linhas'][linha] = {}
        for maquina, src in maquinas.items():
            if maquina not in config['linhas'][linha]:
                config['linhas'][linha][maquina] = {}
            config['linhas'][linha][maquina]['historico_src'] = src
            
    if save_config(config):
        log_user_action(session['username'], "Saved Historicos Backup configurations")
        return jsonify({'status': 'success', 'message': _t('Configurações Históricos salvas com sucesso.')})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar.')})

@app.route('/save_artigos_config', methods=['POST'])
@login_required
def save_artigos_config():
    data = request.get_json()
    config = load_config()
    
    config['artigos_retention_days'] = int(data.get('artigos_retention_days', 365))
    config['artigos_dst_root'] = data.get('artigos_dst_root', '')
    
    if 'linhas' not in config:
        config['linhas'] = {}
        
    for linha, maquinas in data.get('linhas', {}).items():
        if linha not in config['linhas']:
            config['linhas'][linha] = {}
        for maquina, src in maquinas.items():
            if maquina not in config['linhas'][linha]:
                config['linhas'][linha][maquina] = {}
            config['linhas'][linha][maquina]['artigo_src'] = src
            
    if save_config(config):
        log_user_action(session['username'], "Saved Artigos Backup configurations")
        return jsonify({'status': 'success', 'message': _t('Configurações Artigos salvas com sucesso.')})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar.')})

@app.route('/save_mosaic_config', methods=['POST'])
@login_required
def save_mosaic_config_route():
    linha = request.args.get('linha', 'global')
    data = request.get_json()
    full_config = load_mosaic_config()
    
    if linha.startswith('overview_'):
        view = linha.split('_')[1]
        if 'overview' not in full_config: full_config['overview'] = {}
        full_config['overview'][view] = data
    elif linha == 'global': 
        full_config.update(data)
    else: 
        if f"linha_{linha}" not in full_config:
            full_config[f"linha_{linha}"] = {}
        full_config[f"linha_{linha}"].update(data)
        
    if safe_save_json(get_mosaic_config_path(), full_config):
        log_user_action(session['username'], f"Saved mosaic config: {linha}")
        return jsonify({'status': 'success', 'message': f"{_t('Salvo')} ({linha})."})
    return jsonify({'status': 'error', 'message': _t('Erro ao salvar configurações do mosaico.')})

@app.route('/api/mosaic_config')
@login_required
def get_mosaic_config():
    linha = request.args.get('linha', 'global')
    config = load_mosaic_config()
    
    if linha.startswith('overview_'):
        view = linha.split('_')[1]
        ov_cfg = config.get('overview', {}).get(view, {})
        ov_cfg['all_available_machines'] = {}
        sys_cfg = load_config()
        for l, m_dict in sys_cfg.get('linhas', {}).items():
            ov_cfg['all_available_machines'][l] = [m for m, c in m_dict.items() if isinstance(c, dict)]
        return jsonify(ov_cfg)
        
    if linha != 'global' and f"linha_{linha}" in config: return jsonify(config[f"linha_{linha}"])
    return jsonify({k: v for k, v in config.items() if not k.startswith("linha_")})

def find_xml_fields(base_path):
    xml_fields = set()
    sample_count = 0
    if not os.path.exists(base_path): return []
    for root, _, files in os.walk(base_path):
        for file in files:
            if file.lower().endswith('.xml'):
                try:
                    tree = ET.parse(os.path.join(root, file))
                    for element in tree.iter(): xml_fields.add(element.tag)
                    sample_count += 1
                    if sample_count >= 20: break
                except: continue
        if sample_count >= 20: break
    return sorted(list(xml_fields))

def find_unique_values_for_tag(base_path, tag_name):
    unique_values = set()
    sample_count = 0
    if not os.path.exists(base_path): return []
    for root, _, files in os.walk(base_path):
        for file in files:
            if file.lower().endswith('.xml'):
                try:
                    tree = ET.parse(os.path.join(root, file))
                    for element in tree.iter(tag_name):
                        if element.text: unique_values.add(element.text.strip())
                    sample_count += 1
                    if sample_count >= 100: break
                except: continue
        if sample_count >= 100: break
    return sorted(list(unique_values))

@app.route('/analyze_xml_fields', methods=['POST'])
@login_required
def analyze_xml_fields_api():
    path = request.get_json().get('path', '')
    if not path: return jsonify({'error': _t('Caminho não fornecido.')}), 400
    fields = find_xml_fields(path)
    return jsonify(fields)

@app.route('/api/get_xml_tag_values', methods=['POST'])
@login_required
def get_xml_tag_values():
    data = request.get_json()
    values = find_unique_values_for_tag(data.get('path'), data.get('tag'))
    return jsonify({'values': values})

@app.route('/toggle_backup', methods=['POST'])
@login_required
def toggle_backup_route():
    data = request.get_json()
    config = load_config()
    linha = data.get('linha')
    maquina = data.get('maquina')
    key = f"{linha}_{maquina}"
    
    if 'linhas' not in config:
        config['linhas'] = {}
    if linha not in config['linhas']:
        config['linhas'][linha] = {}
    if maquina not in config['linhas'][linha]:
        config['linhas'][linha][maquina] = {}
        
    current_status = config['linhas'][linha][maquina].get('backup_active', False)
    new_status = not current_status
    config['linhas'][linha][maquina]['backup_active'] = new_status
    save_config(config)
    log_user_action(session['username'], f"Toggled backup for {linha}/{maquina} to {'ON' if new_status else 'OFF'}")

    if new_status: 
        start_file_copying_service()
    else:
        if key in copy_threads:
            stop_copy_flags[key] = True
            if copy_threads[key].is_alive():
                copy_threads[key].join(timeout=5)
            del copy_threads[key]
    status_txt = _t("ativado") if new_status else _t("desativado")
    return jsonify({'status': 'success', 'message': f"{_t('Backup')} {status_txt}.", 'active': new_status})

@app.route('/toggle_mosaic', methods=['POST'])
@login_required
def toggle_mosaic_route():
    data = request.get_json()
    config = load_config()
    linha = data.get('linha')
    maquina = data.get('maquina')
    
    if linha == 'Global':
        current_status = config.get('visao_global', {}).get(f'mosaic_{maquina}_active', False)
        new_status = not current_status
        if 'visao_global' not in config: 
            config['visao_global'] = {}
        config['visao_global'][f'mosaic_{maquina}_active'] = new_status
        save_config(config)
        log_user_action(session['username'], f"Toggled global overview mosaic ({maquina}) to {'ON' if new_status else 'OFF'}")
    else:
        if 'linhas' not in config:
            config['linhas'] = {}
        if linha not in config['linhas']:
            config['linhas'][linha] = {}
        if maquina not in config['linhas'][linha]:
            config['linhas'][linha][maquina] = {}
            
        current_status = config['linhas'][linha][maquina].get('mosaic_active', False)
        new_status = not current_status
        config['linhas'][linha][maquina]['mosaic_active'] = new_status
        save_config(config)
        log_user_action(session['username'], f"Toggled mosaic for {linha}/{maquina} to {'ON' if new_status else 'OFF'}")

    if new_status: 
        start_mosaic_process(linha, maquina)
    else: 
        stop_mosaic_process(linha, maquina)
        
    status_txt = _t("ativado") if new_status else _t("desativado")
    return jsonify({'status': 'success', 'message': f"{_t('Mosaico')} {status_txt}.", 'active': new_status})

@app.route('/api/mosaic_status')
@login_required
def get_mosaic_status_api():
    config = load_config()
    statuses = {}
    with mosaic_lock: processes_copy = mosaic_processes.copy()

    for linha, l_config in config.get('linhas', {}).items():
        for maquina, m_config in l_config.items():
            if not isinstance(m_config, dict): continue
            key = f"{linha}_{maquina}"
            process = processes_copy.get(key)
            is_running = process and process.poll() is None
            statuses[key] = {
                'linha': linha, 'maquina': maquina, 'maquina_display': _t(maquina.capitalize()),
                'is_configured': m_config.get('mosaic_active', False),
                'is_running': is_running,
                'pid': process.pid if is_running else None,
                'port': m_config.get('mosaic_port')
            }
            
    vg_config = config.get('visao_global', {})
    if vg_config:
        for maq, p_key in [("lateral", "port_lateral"), ("fundo", "port_fundo")]:
            key = f"Global_{maq}"
            process = processes_copy.get(key)
            is_running = process and process.poll() is None
            statuses[key] = {
                'linha': 'Global', 'maquina': maq, 'maquina_display': _t(maq.capitalize()),
                'is_configured': vg_config.get(f'mosaic_{maq}_active', False),
                'is_running': is_running,
                'pid': process.pid if is_running else None,
                'port': vg_config.get(p_key)
            }
    return jsonify({'mosaics': statuses})

@app.route('/api/mosaic_control/<action>', methods=['POST'])
@login_required
def mosaic_control_api(action):
    data = request.get_json()
    linha, maquina = data['linha'], data['maquina']
    log_user_action(session['username'], f"Sent mosaic command '{action}' to {linha}/{maquina}")
    if action == 'start': start_mosaic_process(linha, maquina)
    elif action == 'stop': stop_mosaic_process(linha, maquina)
    elif action == 'restart':
        stop_mosaic_process(linha, maquina)
        time.sleep(1)
        start_mosaic_process(linha, maquina)
    return jsonify({'status': 'success', 'message': f"{_t('Comando')} {action} {_t('enviado.')}"})

@app.route('/start_all', methods=['POST'])
@login_required
def start_all():
    log_user_action(session['username'], "Issued START ALL services")
    start_file_copying_service()
    start_mirror_ssd_service()
    start_pkiris_service()
    start_historicos_service()
    start_artigos_service()
    start_all_active_mosaics()
    start_public_portal()
    start_pen_pkiris_portal()
    return jsonify({'status': 'success', 'message': _t('Iniciados.')})

@app.route('/stop_all', methods=['POST'])
@login_required
def stop_all():
    log_user_action(session['username'], "Issued STOP ALL services")
    stop_file_copying_service()
    stop_mirror_ssd_service()
    stop_pkiris_service()
    stop_historicos_service()
    stop_artigos_service()
    stop_all_mosaic_processes()
    stop_public_portal()
    stop_pen_pkiris_portal()
    return jsonify({'status': 'success', 'message': _t('Parados.')})

@app.route('/diagnostics')
@login_required
def diagnostics():
    with counters_lock:
        total_s_jpg = sum(c.get('jpg',0) for c in files_copied_shift.values())
        total_s_xml = sum(c.get('xml',0) for c in files_copied_shift.values())
        total_d_jpg = sum(c.get('jpg',0) for c in files_copied_day.values())
        total_d_xml = sum(c.get('xml',0) for c in files_copied_day.values())

    return jsonify({
        'current_shift': get_current_shift(),
        'storage': {'volume1': get_disk_usage('/volume1'), 'volume2': get_disk_usage('/volume2')},
        'system': {'cpu_percent': psutil.cpu_percent(), 'memory_percent': psutil.virtual_memory().percent},
        'services': {
            'Mirror SSD': mirror_thread and mirror_thread.is_alive(),
            'Cópia de Arquivos': any(t.is_alive() for t in copy_threads.values()),
            'Mosaicos': any(p.poll() is None for p in mosaic_processes.values()),
            'Backup PKIRIS': pkiris_thread and pkiris_thread.is_alive(),
            'Backup Históricos': historicos_thread and historicos_thread.is_alive(),
            'Backup Artigos': artigos_thread and artigos_thread.is_alive(),
            'Portal Público': public_portal_process and public_portal_process.poll() is None,
            'Portal Pen PKIRIS': pen_pkiris_process and pen_pkiris_process.poll() is None
        },
        'total_shift_jpg': total_s_jpg, 'total_shift_xml': total_s_xml,
        'total_day_jpg': total_d_jpg, 'total_day_xml': total_d_xml
    })

def get_actual_log_path():
    config_log = load_config().get("log_file_path", "")
    if not config_log:
        return LOG_FILE
    if os.path.isdir(config_log):
        return os.path.join(config_log, "backup_server.log")
    return config_log

@app.route('/logs')
@login_required
def get_logs():
    try:
        current_log = get_actual_log_path()
        if os.path.exists(current_log):
            with open(current_log, 'r', encoding='utf-8', errors='replace') as f:
                return jsonify({'logs': f.readlines()[-200:]})
        return jsonify({'logs': []})
    except Exception as e:
        return jsonify({'logs': [f"{_t('Erro na leitura dos logs:')} {str(e)}"]})

@app.route('/clear_logs', methods=['POST'])
@login_required
def clear_logs_route():
    try:
        current_log = get_actual_log_path()
        log_user_action(session['username'], f"Cleared system logs")
        open(current_log, 'w', encoding='utf-8').close()
        return jsonify({'status': 'success', 'message': _t('Logs limpos.')})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f"Erro: {str(e)}"})

@app.route('/download_logs')
@login_required
def download_logs():
    current_log = get_actual_log_path()
    log_user_action(session['username'], "Downloaded system logs")
    return send_file(current_log, as_attachment=True)

def initial_startup():
    logging.info("A iniciar serviços em segundo plano...")
    time.sleep(2)
    start_all_active_mosaics()
    start_file_copying_service()
    start_mirror_ssd_service()
    start_pkiris_service()
    start_historicos_service()
    start_artigos_service()
    start_public_portal()
    start_pen_pkiris_portal()
    logging.info("Inicialização concluída.")

if __name__ == '__main__':
    logging.info("A Iniciar...")
    startup_thread = threading.Thread(target=initial_startup)
    startup_thread.daemon = True
    startup_thread.start()
    
    app.run(host='0.0.0.0', port=5580, debug=False, threaded=True, use_reloader=False)