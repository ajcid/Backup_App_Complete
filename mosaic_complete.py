#!/usr/bin/env python3
"""
Servidor Flask para Exibição de Mosaico de Imagens de Inspeção.
Este servidor é iniciado pelo aplicativo principal de backup e serve uma página web
para uma linha/máquina específica, baseando-se nas configurações compartilhadas.

ATUALIZAÇÃO: Histórico avançado carrega os dois últimos turnos com barra de tempo dividida,
ticks de 30 minutos e lazy-loading de max 50 imagens. Resolução dinâmica de caminhos (Produção/Teste).
Tradução profunda de Câmaras e Máquinas lida globalmente via Cookie (sem interface de troca).
Código HTML/CSS totalmente expandido para máxima legibilidade.
CORREÇÃO DE PERFORMANCE MÁXIMA (Zero I/O Thrashing):
- Bloqueio de threads (Thread Lock) na cache do diretório.
- Mapeamento simultâneo de JPGs e XMLs na RAM num único os.scandir para evitar chamadas os.path.exists.
- Overview configurada para respeitar o refresh_interval (fim dos ataques de 1 segundo).
"""

import os
import sys
import json
import glob
import logging
import copy
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import psutil
import atexit
import platform
import threading
import time
import re
from flask import Flask, render_template_string, jsonify, send_from_directory, abort, make_response, request, redirect, url_for, session, has_request_context

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS PRINCIPAIS (DINÂMICOS E MULTIPLATAFORMA)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

BASE_CONFIG_PATH = DATA_DIR
BASE_LOG_PATH = os.path.join(DATA_DIR, "logs")

os.makedirs(BASE_CONFIG_PATH, exist_ok=True)
os.makedirs(BASE_LOG_PATH, exist_ok=True)

CONFIG_FILE = os.path.join(BASE_CONFIG_PATH, "backup_settings.json")
MOSAIC_CONFIG_FILE = os.path.join(BASE_CONFIG_PATH, "mosaic_settings.json")

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ==============================================================================
# EXPRESSÕES REGULARES PARA PARSING RÁPIDO (POUPANÇA DE CPU)
# ==============================================================================
RE_XML_CAM = re.compile(r'<NUM_CAM[^>]*>(.*?)</NUM_CAM>', re.IGNORECASE)

# ==============================================================================
# SISTEMA DE INTERNACIONALIZAÇÃO E CACHES GLOBAIS
# ==============================================================================
SUPPORTED_LANGUAGES = ['pt', 'es', 'en', 'pl', 'bg']
DEFAULT_LANG = 'pt'

_translations_cache = {}
_translations_mtime = {}
_xml_cache = {}

_dir_cache = {}
_dir_cache_time = {}
_dir_cache_lock = threading.Lock()

def get_cached_dir(path):
    """
    Lê a diretoria e mapeia JPGs e XMLs de uma só vez. 
    Faz cache na RAM durante 10 segundos, com Lock seguro para múltiplas threads.
    Elimina a necessidade de fazer os.path.exists() para ficheiros XML individuais.
    """
    now = time.time()
    
    with _dir_cache_lock:
        if path in _dir_cache and (now - _dir_cache_time.get(path, 0) < 10.0):
            return _dir_cache[path]
            
        data = {'jpgs': [], 'xmls': set()}
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_file():
                        lower_name = entry.name.lower()
                        if lower_name.endswith(('.jpg', '.jpeg')):
                            data['jpgs'].append({
                                "path": entry.path,
                                "name": entry.name,
                                "mtime": entry.stat().st_mtime
                            })
                        elif lower_name.endswith('.xml'):
                            data['xmls'].add(entry.name)
        except OSError:
            pass
            
        _dir_cache[path] = data
        _dir_cache_time[path] = now
        return data

def load_translations(lang):
    path = os.path.join(DATA_DIR, f'lang_{lang}.json')
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else 0
        if lang in _translations_cache and _translations_mtime.get(lang) == mtime:
            return _translations_cache[lang]
            
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'r', encoding='utf-8') as f:
                _translations_cache[lang] = json.load(f)
                _translations_mtime[lang] = mtime
                return _translations_cache[lang]
    except Exception as e:
        logging.error(f"Erro ao carregar traduções {path}: {e}")
    return {}

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

def load_json_config(file_path):
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception as e:
        logging.error(f"Erro ao carregar arquivo JSON {file_path}: {e}")
        return {}

def get_merged_mosaic_config(linha, maquina=None):
    mosaic_cfg = load_json_config(MOSAIC_CONFIG_FILE)
    if not isinstance(mosaic_cfg, dict): 
        mosaic_cfg = {}
    
    def safe_dict(d):
        return copy.deepcopy(d) if isinstance(d, dict) else {}

    merged = {
        "display_config": safe_dict(mosaic_cfg.get("display_config")),
        "xml_config": safe_dict(mosaic_cfg.get("xml_config")),
        "overlay_config": safe_dict(mosaic_cfg.get("overlay_config")),
        "filter_config": safe_dict(mosaic_cfg.get("filter_config"))
    }
    
    line_cfg = mosaic_cfg.get(f"linha_{linha}")
    if isinstance(line_cfg, dict):
        for key in merged.keys():
            if isinstance(line_cfg.get(key), dict):
                merged[key].update(copy.deepcopy(line_cfg[key]))
                
    if maquina:
        machine_cfg = mosaic_cfg.get(f"linha_{linha}_{maquina}")
        if isinstance(machine_cfg, dict):
            for key in merged.keys():
                if isinstance(machine_cfg.get(key), dict):
                    merged[key].update(copy.deepcopy(machine_cfg[key]))
                    
    return merged

def get_active_dst_path(config, linha, maquina):
    linha_cfg = config.get('linhas', {}).get(linha, {})
    if not isinstance(linha_cfg, dict): 
        return None
        
    maquina_cfg = linha_cfg.get(maquina, {})
    if not isinstance(maquina_cfg, dict): 
        return None
        
    use_test = linha_cfg.get('use_test_mode', False)
    
    if use_test:
        dst = maquina_cfg.get('dst_test')
        return dst if dst else maquina_cfg.get('dst')
    else:
        dst = maquina_cfg.get('dst_prod')
        return dst if dst else maquina_cfg.get('dst')

def safe_int(value, default_val):
    try:
        if value is None or value == "":
            return default_val
        return int(value)
    except (ValueError, TypeError):
        return default_val

def get_shift_order(main_cfg):
    turnos = main_cfg.get('turnos', {})
    if not turnos:
        return ['turno1', 'turno2', 'turno3']
    sorted_turnos = sorted(turnos.items(), key=lambda item: item[1].get('inicio', '00:00') if isinstance(item[1], dict) else '00:00')
    return [turno[0] for turno in sorted_turnos]

def generate_search_paths(base_path, main_cfg):
    if not base_path:
        return
        
    shift_order = get_shift_order(main_cfg)
    current_shift_name = get_current_shift()
    
    current_date = datetime.now()
    try:
        shift_index = shift_order.index(current_shift_name)
    except ValueError:
        shift_index = 0

    for _ in range(7):
        date_str = current_date.strftime("%Y-%m-%d")
        for j in range(shift_index, -1, -1):
            shift_name = shift_order[j]
            yield os.path.join(base_path, date_str, shift_name)
            
        shift_index = len(shift_order) - 1
        current_date -= timedelta(days=1)

def get_current_shift():
    main_cfg = load_json_config(CONFIG_FILE)
    turnos = main_cfg.get('turnos', {})
    now = datetime.now()
    current_time = now.strftime('%H:%M')
    for turno_name, turno_config in turnos.items():
        if not isinstance(turno_config, dict):
            continue
        inicio = turno_config.get('inicio', '06:00')
        fim = turno_config.get('fim', '14:00')
        if inicio > fim:
            if current_time >= inicio or current_time < fim: 
                return turno_name
        else:
            if inicio <= current_time < fim: 
                return turno_name
    return 'turno1'

def get_current_and_prev_shift_ranges(config):
    now = datetime.now()
    turnos = config.get('turnos', {})
    
    curr_shift = None
    curr_start_dt = None
    curr_end_dt = None
    
    for t_name, t_cfg in turnos.items():
        start_str = t_cfg.get('inicio', '00:00')
        end_str = t_cfg.get('fim', '23:59')
        st = datetime.strptime(start_str, "%H:%M").time()
        et = datetime.strptime(end_str, "%H:%M").time()
        
        dt_start = datetime.combine(now.date(), st)
        dt_end = datetime.combine(now.date(), et)
        
        if st > et:
            if now.time() < et:
                dt_start -= timedelta(days=1)
            else:
                dt_end += timedelta(days=1)
                
        if dt_start <= now < dt_end:
            curr_shift = t_name
            curr_start_dt = dt_start
            curr_end_dt = dt_end
            break
            
    if not curr_shift:
        curr_shift = list(turnos.keys())[0] if turnos else 'turno1'
        curr_start_dt = now - timedelta(hours=8)
        curr_end_dt = now
        
    prev_end_dt = curr_start_dt
    prev_shift = None
    prev_start_dt = None
    
    for t_name, t_cfg in turnos.items():
        if t_name == curr_shift: continue
        end_str = t_cfg.get('fim', '23:59')
        start_str = t_cfg.get('inicio', '00:00')
        et = datetime.strptime(end_str, "%H:%M").time()
        st = datetime.strptime(start_str, "%H:%M").time()
        
        if et == prev_end_dt.time():
            prev_shift = t_name
            prev_start_dt = datetime.combine(prev_end_dt.date(), st)
            if st > et:
                prev_start_dt -= timedelta(days=1)
            break
            
    if not prev_shift:
        shifts = get_shift_order(config)
        try:
            idx = shifts.index(curr_shift)
            prev_shift = shifts[(idx - 1) % len(shifts)]
        except:
            prev_shift = curr_shift
        prev_start_dt = prev_end_dt - timedelta(hours=8)
        
    return curr_shift, curr_start_dt, curr_end_dt, prev_shift, prev_start_dt, prev_end_dt

def safe_getmtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0

def get_camera_dicts():
    fundo_cameras = {
        "21": {"nome": _t("Boca 2"), "cor": "#e74c3c"},
        "25": {"nome": _t("Boca 1"), "cor": "#e67e22"},
        "13": {"nome": _t("Stress"), "cor": "#f1c40f"},
        "28": {"nome": _t("Fundo 2"), "cor": "#2ecc71"},
        "11": {"nome": _t("Fundo"), "cor": "#27ae60"},
        "15": {"nome": _t("Leitor 2"), "cor": "#3498db"},
        "24": {"nome": _t("Leitor"), "cor": "#2980b9"},
        "22": {"nome": _t("Wire Edge"), "cor": "#9b59b6"},
        "indefinido": {"nome": _t("Indef."), "cor": "#95a5a6"}
    }
    
    lateral_normal_top = {
        "13": {"nome": f"{_t('Câmara')} 13", "cor": "#3498db"},
        "33": {"nome": f"{_t('Câmara')} 33", "cor": "#00bcd4"},
        "14": {"nome": f"{_t('Câmara')} 14", "cor": "#2ecc71"},
        "23": {"nome": f"{_t('Câmara')} 23", "cor": "#8bc34a"},
        "34": {"nome": f"{_t('Câmara')} 34", "cor": "#f1c40f"},
        "24": {"nome": f"{_t('Câmara')} 24", "cor": "#ff9800"}
    }
    
    lateral_normal_bottom = {
        "11": {"nome": f"{_t('Câmara')} 11", "cor": "#e91e63"},
        "31": {"nome": f"{_t('Câmara')} 31", "cor": "#9c27b0"},
        "12": {"nome": f"{_t('Câmara')} 12", "cor": "#3f51b5"},
        "21": {"nome": f"{_t('Câmara')} 21", "cor": "#009688"},
        "32": {"nome": f"{_t('Câmara')} 32", "cor": "#795548"},
        "22": {"nome": f"{_t('Câmara')} 22", "cor": "#607d8b"}
    }
    
    lateral_stress = {
        "41": {"nome": f"{_t('Stress')} 41", "cor": "#c0392b"},
        "42": {"nome": f"{_t('Stress')} 42", "cor": "#e74c3c"},
        "43": {"nome": f"{_t('Stress')} 43", "cor": "#d2b4de"},
        "44": {"nome": f"{_t('Stress')} 44", "cor": "#f5b041"}
    }
    
    lateral_cameras_all = {**lateral_normal_top, **lateral_normal_bottom, **lateral_stress}
    lateral_cameras_all["indefinido"] = {"nome": _t("Indef."), "cor": "#95a5a6"}
    
    return fundo_cameras, lateral_normal_top, lateral_normal_bottom, lateral_stress, lateral_cameras_all


# --- Variáveis Globais ---
SERVER_PORT = 5000
LINHA = "N/A"
MAQUINA = "N/A"

# ==============================================================================
# TEMPLATES HTML EXPANDIDOS (TOTALMENTE SEGUROS SEM MINIFICAÇÃO)
# ==============================================================================
MOSAIC_TEMPLATE = """
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
    <meta charset="UTF-8">
    <title>{{ t('Mosaico de Imagens') }} - {{ t('Linha') }} {{ linha }} / {{ t(maquina.capitalize()) }}</title>
    <style>
        body, html { 
            margin: 0; 
            padding: 0; 
            width: 100vw; 
            height: 100vh; 
            overflow: hidden; 
            background-color: #2c3e50; 
            font-family: sans-serif; 
            color: #ecf0f1; 
        }
        
        #app-wrapper { 
            position: absolute; 
            top: 50%; 
            left: 50%; 
            overflow: auto; 
            box-sizing: border-box; 
            padding: 20px; 
        }
        
        .rot-0 { 
            width: 100vw; 
            height: 100vh; 
            transform: translate(-50%, -50%) rotate(0deg); 
        }
        .rot-90 { 
            width: 100vh; 
            height: 100vw; 
            transform: translate(-50%, -50%) rotate(90deg); 
        }
        .rot-180 { 
            width: 100vw; 
            height: 100vh; 
            transform: translate(-50%, -50%) rotate(180deg); 
        }
        .rot-270 { 
            width: 100vh; 
            height: 100vw; 
            transform: translate(-50%, -50%) rotate(270deg); 
        }

        .header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding-bottom: 15px; 
            border-bottom: 2px solid #34495e; 
            position: relative; 
            text-align: left; 
        }
        
        .header-left { 
            display: flex; 
            flex-direction: column; 
            align-items: flex-start; 
        }
        
        .header-right { 
            display: flex; 
            flex-direction: column; 
            align-items: flex-end; 
            max-width: 70%; 
        }
        
        .header h1 { 
            margin: 0; 
            font-size: 1.8rem; 
            color: #3498db; 
        }
        
        .header p { 
            margin: 4px 0; 
            color: #bdc3c7; 
            font-size: 0.9rem; 
        }
        
        .legend-container { 
            display: flex; 
            flex-wrap: wrap; 
            gap: 4px; 
            justify-content: flex-end; 
            margin-top: 5px; 
        }
        
        .legend-item { 
            display: flex; 
            align-items: center; 
            font-size: 0.7rem; 
            background: #1e272e; 
            padding: 2px 6px; 
            border-radius: 4px; 
            border: 1px solid #34495e; 
            color: #ecf0f1; 
            font-weight: bold;
        }
        
        .legend-color { 
            width: 10px; 
            height: 10px; 
            border-radius: 50%; 
            margin-right: 4px; 
            flex-shrink: 0; 
        }
        
        .header-buttons { 
            margin-top: 10px; 
            display: flex; 
            gap: 10px; 
            align-items: center; 
        }
        
        .btn-action { 
            padding: 8px 16px; 
            color: white; 
            border: none; 
            border-radius: 4px; 
            cursor: pointer; 
            font-weight: bold; 
            font-size: 13px; 
            transition: background 0.3s; 
            text-decoration: none; 
        }
        
        .btn-history { 
            background-color: #3498db; 
        }
        
        .btn-history:hover { 
            background-color: #2980b9; 
        }

        .modal-bg { 
            display: none; 
            position: absolute; 
            z-index: 2000; 
            left: 0; 
            top: 0; 
            width: 100%; 
            height: 100%; 
            background-color: rgba(0,0,0,0.8); 
            justify-content: center; 
            align-items: center; 
        }
        
        .modal-bg.visible { 
            display: flex; 
        }
        
        .modal-box { 
            background-color: #2c3e50; 
            padding: 30px; 
            border-radius: 8px; 
            border: 1px solid #34495e; 
            width: 400px; 
            max-width: 90%; 
            text-align: center; 
            position: relative; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5); 
        }
        
        .close-modal { 
            position: absolute; 
            top: 10px; 
            right: 15px; 
            color: #ecf0f1; 
            font-size: 24px; 
            cursor: pointer; 
            font-weight: bold; 
        }
        
        .close-modal:hover { 
            color: #e74c3c; 
        }
        
        .machine-list { 
            display: flex; 
            flex-direction: column; 
            gap: 10px; 
            margin-top: 20px; 
        }
        
        .machine-btn { 
            padding: 12px; 
            background-color: #2ecc71; 
            color: white; 
            border: none; 
            border-radius: 4px; 
            cursor: pointer; 
            font-size: 16px; 
            transition: background 0.3s; 
            text-transform: capitalize; 
        }
        
        .machine-btn:hover { 
            background-color: #27ae60; 
        }

        #mosaic-grid { 
            display: grid; 
            grid-template-columns: repeat({{ grid_columns }}, 1fr); 
            grid-auto-rows: {{ image_size }}px; 
            gap: 15px; 
            padding-top: 20px; 
        }
        
        .grid-item { 
            position: relative; 
            background-color: #1e272e; 
            border-radius: 8px; 
            overflow: hidden; 
            box-shadow: 0 4px 8px rgba(0,0,0,0.3); 
            cursor: zoom-in; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            box-sizing: border-box; 
            border: 4px solid transparent; 
            transition: border-color 0.3s; 
        }
        
        .grid-item img { 
            width: 100%; 
            height: 100%; 
            display: block; 
            object-fit: contain; 
            object-position: center; 
        }
        
        .overlay { 
            position: absolute; 
            bottom: 0; 
            left: 0; 
            right: 0; 
            background: rgba(0, 0, 0, 0.7); 
            padding: 8px; 
            font-size: 12px; 
            display: flex; 
            flex-direction: column; 
            color: #fff; 
            pointer-events: none; 
        }
        
        .overlay-top { 
            position: absolute; 
            top: 0; 
            left: 0; 
            right: 0; 
            background: rgba(0, 0, 0, 0.7); 
            padding: 8px; 
            font-size: 14px; 
            font-weight: bold; 
            text-align: center; 
            pointer-events: none; 
            color: #f1c40f;
        }
        
        .overlay-bottom { 
            display: flex; 
            justify-content: space-between; 
            width: 100%; 
        }
        
        .status-message { 
            text-align: center; 
            color: #e74c3c; 
            font-size: 1.2rem; 
            padding: 2rem; 
            grid-column: 1 / -1; 
        }
        
        #zoom-container { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            z-index: 9999; 
            justify-content: center; 
            align-items: center; 
            cursor: zoom-out; 
            background: rgba(0, 0, 0, 0.9); 
        }
        
        #zoom-container.visible { 
            display: flex; 
        }
    </style>
</head>
<body>
    <div id="app-wrapper" class="rot-{{ rotation }}">
        <div class="header">
            <div class="header-left">
                <h1>{{ t('Mosaico de Imagens') }}</h1>
                <p>{{ t('Linha') }}: <strong>{{ linha }}</strong> | {{ t('Máquina') }}: <strong>{{ t(maquina.capitalize()) }}</strong> | {{ t('Porta') }}: <strong>{{ port }}</strong></p>
                <p style="font-size: 0.8rem;">{{ t('Atualizando a cada') }} <span id="refresh-interval">{{ refresh_interval }}</span>s. <span id="cycle-status"></span></p>
                <div class="header-buttons">
                    <button class="btn-action btn-history" onclick="openHistoryModal()">{{ t('Visualizar Histórico') }}</button>
                </div>
            </div>
            
            <div class="header-right">
                <div style="font-size: 11px; color: #bdc3c7; margin-bottom: 2px; font-weight: bold; text-transform: uppercase;">
                    {{ t('Legenda de Câmaras de Rejeição') }}
                </div>
                {% if is_fundo %}
                <div class="legend-container">
                    {% for cam_id, cam_info in fundo_cameras.items() %}
                        <div class="legend-item">
                            <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                            {{ cam_info.nome }} ({{ cam_id }})
                        </div>
                    {% endfor %}
                </div>
                {% elif is_lateral %}
                <div style="display: flex; border-radius: 6px; overflow: hidden; border: 1px solid #34495e; margin-top: 2px;">
                    <div style="background-color: #ecf0f1; padding: 4px; display: flex; flex-direction: column; gap: 4px;">
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_normal_top.items() %}
                                <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid {{ cam_info.cor }};">
                                    <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                    {{ cam_id }}
                                </div>
                            {% endfor %}
                        </div>
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_normal_bottom.items() %}
                                <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid {{ cam_info.cor }};">
                                    <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                    {{ cam_id }}
                                </div>
                            {% endfor %}
                            <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid #95a5a6;">
                                <div class="legend-color" style="background-color: #95a5a6;"></div>
                                {{ t('Indef.') }}
                            </div>
                        </div>
                    </div>
                    <div style="background-color: #111; padding: 4px; display: flex; flex-direction: column; gap: 4px; border-left: 2px solid #34495e;">
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index <= 2 %}
                                    <div class="legend-item" style="background: #2c3e50; border: 1px solid #111; border-bottom: 3px solid {{ cam_info.cor }};">
                                        <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                        {{ cam_id }}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index > 2 %}
                                    <div class="legend-item" style="background: #2c3e50; border: 1px solid #111; border-bottom: 3px solid {{ cam_info.cor }};">
                                        <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                        {{ cam_id }}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                    </div>
                </div>
                {% endif %}
            </div>
        </div>
        
        <div id="mosaic-grid"></div>
        <div id="zoom-container"></div>

        <div id="history-modal" class="modal-bg">
            <div class="modal-box">
                <span class="close-modal" onclick="closeHistoryModal()">&times;</span>
                <h2>{{ t('Visualizar Histórico') }}</h2>
                <p>{{ t('Selecione a máquina da') }} <strong>{{ t('Linha') }} {{ linha }}</strong>:</p>
                <div class="machine-list">
                    {% for m in maquinas %}
                    <button class="machine-btn" onclick="goToHistory('{{ m }}')">{{ t(m.capitalize()) }}</button>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>

    <script>
        const REFRESH_INTERVAL = {{ refresh_interval * 1000 }};
        const ZOOM_PERCENTAGE = {{ zoom_percentage }};
        const BASE_IMAGE_SIZE = {{ image_size }};
        window.CURRENT_IMAGE_SIZE = BASE_IMAGE_SIZE;
        
        const fundoCameras = {{ fundo_cameras | tojson | safe if fundo_cameras else '{}' }};
        const lateralCameras = {{ lateral_cameras_all | tojson | safe if lateral_cameras_all else '{}' }};
        const isFundo = {{ 'true' if is_fundo else 'false' }};
        const isLateral = {{ 'true' if is_lateral else 'false' }};

        function openHistoryModal() { 
            document.getElementById('history-modal').classList.add('visible'); 
        }
        
        function closeHistoryModal() { 
            document.getElementById('history-modal').classList.remove('visible'); 
        }
        
        function goToHistory(maquinaSelecionada) { 
            window.location.href = '/historico/' + maquinaSelecionada; 
        }

        function fetchImages() {
            if (document.body.classList.contains('zoomed-active')) return;
            
            fetch('/api/images')
                .then(response => response.json())
                .then(data => {
                    if (data.orientation !== undefined) {
                        document.getElementById('app-wrapper').className = 'rot-' + data.orientation;
                    }
                    const grid = document.getElementById('mosaic-grid');
                    if (data.grid_columns !== undefined) {
                        grid.style.gridTemplateColumns = `repeat(${data.grid_columns}, 1fr)`;
                    }
                    if (data.image_size !== undefined) {
                        grid.style.gridAutoRows = `${data.image_size}px`;
                        window.CURRENT_IMAGE_SIZE = data.image_size;
                    }
                    
                    grid.innerHTML = '';
                    
                    if (data.error) { 
                        grid.innerHTML = `<p class="status-message">${data.error}</p>`; 
                        return; 
                    }
                    if (data.images.length === 0) { 
                        grid.innerHTML = `<p class="status-message">{{ t('Nenhuma imagem encontrada para o turno atual ou para o filtro selecionado.') }}</p>`; 
                        return; 
                    }

                    data.images.forEach(item => {
                        const gridItem = document.createElement('div');
                        gridItem.className = 'grid-item';
                        
                        let cId = item.cam_id ? item.cam_id.toString().trim() : 'indefinido';
                        let borderCol = '#34495e'; 
                        if (isFundo && fundoCameras[cId]) {
                            borderCol = fundoCameras[cId].cor;
                        } else if (isLateral && lateralCameras[cId]) {
                            borderCol = lateralCameras[cId].cor;
                        }
                        gridItem.style.border = `4px solid ${borderCol}`;
                        
                        const img = document.createElement('img');
                        img.src = item.url;
                        img.onerror = () => { img.src = "https://placehold.co/300x300/34495e/ecf0f1?text=Imagem+Faltando"; };
                        gridItem.appendChild(img);
                        
                        if (item.overlay_top) {
                            const overlayTop = document.createElement('div');
                            overlayTop.className = 'overlay-top';
                            overlayTop.textContent = item.overlay_top;
                            gridItem.appendChild(overlayTop);
                        }
                        
                        const overlay = document.createElement('div');
                        overlay.className = 'overlay';
                        
                        const overlayBottom = document.createElement('div');
                        overlayBottom.className = 'overlay-bottom';
                        
                        const bottomLeft = document.createElement('span');
                        bottomLeft.textContent = item.overlay_bottom_left || '';
                        overlayBottom.appendChild(bottomLeft);
                        
                        const bottomRight = document.createElement('span');
                        bottomRight.textContent = item.overlay_bottom_right || '';
                        overlayBottom.appendChild(bottomRight);
                        
                        overlay.appendChild(overlayBottom);
                        gridItem.appendChild(overlay);
                        
                        gridItem.addEventListener('dblclick', (event) => {
                            const zoomContainer = document.getElementById('zoom-container');
                            zoomContainer.innerHTML = '';
                            
                            const zoomWrapper = document.createElement('div');
                            zoomWrapper.style.position = 'relative';
                            zoomWrapper.style.width = `${window.CURRENT_IMAGE_SIZE}px`; 
                            zoomWrapper.style.borderRadius = '8px';
                            zoomWrapper.style.overflow = 'hidden';
                            zoomWrapper.style.boxShadow = '0 10px 50px rgba(0,0,0,0.9)';
                            zoomWrapper.style.backgroundColor = '#34495e';
                            zoomWrapper.style.transform = `scale(${ZOOM_PERCENTAGE / 100})`;
                            zoomWrapper.style.transformOrigin = 'center center';
                            zoomWrapper.style.border = `8px solid ${borderCol}`;
                            zoomWrapper.style.boxSizing = 'border-box';
                            
                            const zoomedImg = document.createElement('img');
                            zoomedImg.src = item.url;
                            zoomedImg.style.width = '100%';
                            zoomedImg.style.display = 'block';
                            zoomWrapper.appendChild(zoomedImg);
                            
                            if (item.overlay_top) {
                                const t = document.createElement('div');
                                t.className = 'overlay-top';
                                t.textContent = item.overlay_top;
                                zoomWrapper.appendChild(t);
                            }
                            
                            const o = document.createElement('div');
                            o.className = 'overlay';
                            
                            const ob = document.createElement('div');
                            ob.className = 'overlay-bottom';
                            
                            const spanBl = document.createElement('span');
                            spanBl.textContent = item.overlay_bottom_left || '';
                            
                            const spanBr = document.createElement('span');
                            spanBr.textContent = item.overlay_bottom_right || '';
                            
                            ob.appendChild(spanBl);
                            ob.appendChild(spanBr);
                            o.appendChild(ob);
                            zoomWrapper.appendChild(o);
                            
                            zoomContainer.appendChild(zoomWrapper);
                            zoomContainer.classList.add('visible');
                            document.body.classList.add('zoomed-active');
                        });
                        
                        grid.appendChild(gridItem);
                    });
                })
                .catch(err => console.error("Erro no fetch de images:", err));
        }
        
        function closeZoom() {
            const zoomContainer = document.getElementById('zoom-container');
            zoomContainer.classList.remove('visible');
            document.body.classList.remove('zoomed-active');
            zoomContainer.innerHTML = '';
        }

        function checkForCycle() {
            fetch('/api/cycle_info')
                .then(response => response.json())
                .then(data => {
                    if (data.cycle_active) {
                        const statusElem = document.getElementById('cycle-status');
                        if (statusElem) {
                            statusElem.innerHTML = `| <strong>{{ t('Modo Ciclo Ativo.') }}</strong> {{ t('Próxima:') }} ${data.next_machine}`;
                            statusElem.style.color = '#f1c40f';
                        }
                        setTimeout(function attemptRedirect() {
                            if (!document.body.classList.contains('zoomed-active')) {
                                window.location.href = `http://${window.location.hostname}:${data.next_port}`;
                            } else {
                                setTimeout(attemptRedirect, 2000); 
                            }
                        }, data.cycle_interval_sec * 1000);
                    } else {
                        setTimeout(checkForCycle, 10000); 
                    }
                })
                .catch(err => {
                    console.error('Could not fetch cycle info:', err);
                    setTimeout(checkForCycle, 10000); 
                });
        }

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('zoom-container').addEventListener('click', closeZoom);
            fetchImages();
            setInterval(fetchImages, REFRESH_INTERVAL);
            checkForCycle();
        });
    </script>
</body>
</html>
"""

OVERVIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ t('Visão Global das Linhas') }}</title>
    <style>
        body, html { 
            font-family: sans-serif; 
            background-color: #000; 
            color: #ecf0f1; 
            margin: 0; 
            padding: 0; 
            width: 100vw; 
            height: 100vh; 
            overflow: hidden; 
        }
        
        #app-wrapper { 
            position: absolute; 
            top: 50%; 
            left: 50%; 
            display: flex; 
            flex-direction: column; 
            box-sizing: border-box; 
        }
        
        .rot-0 { 
            width: 100vw; 
            height: 100vh; 
            transform: translate(-50%, -50%) rotate(0deg); 
        }
        .rot-90 { 
            width: 100vh; 
            height: 100vw; 
            transform: translate(-50%, -50%) rotate(90deg); 
        }
        .rot-180 { 
            width: 100vw; 
            height: 100vh; 
            transform: translate(-50%, -50%) rotate(180deg); 
        }
        .rot-270 { 
            width: 100vh; 
            height: 100vw; 
            transform: translate(-50%, -50%) rotate(270deg); 
        }

        .header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 15px; 
            background: #111; 
            flex-shrink: 0; 
            border-bottom: 2px solid #333; 
            position: relative; 
            text-align: left;
        }
        
        .header-left { 
            display: flex; 
            flex-direction: column; 
            align-items: flex-start; 
        }
        
        .header-right { 
            display: flex; 
            flex-direction: column; 
            align-items: flex-end; 
            max-width: 70%; 
        }
        
        .header h1 { 
            margin: 0; 
            font-size: 2rem; 
            color: #f1c40f; 
            text-transform: uppercase; 
            letter-spacing: 2px; 
        }
        
        .legend-container { 
            display: flex; 
            flex-wrap: wrap; 
            gap: 4px; 
            justify-content: flex-end; 
            margin-top: 5px; 
        }
        
        .legend-item { 
            display: flex; 
            align-items: center; 
            font-size: 0.7rem; 
            background: #1e272e; 
            padding: 2px 6px; 
            border-radius: 4px; 
            border: 1px solid #34495e; 
            color: #ecf0f1; 
            font-weight: bold;
        }
        
        .legend-color { 
            width: 10px; 
            height: 10px; 
            border-radius: 50%; 
            margin-right: 4px; 
            flex-shrink: 0; 
        }
        
        .grid-container { 
            padding: 10px; 
            flex-grow: 1; 
            min-height: 0; 
            overflow: hidden; 
        }
        
        .column { 
            background: #1a1a1a; 
            border-radius: 8px; 
            padding: 8px; 
            min-height: 0; 
        }
        
        .col-title { 
            text-align: center; 
            background: #2c3e50; 
            border-radius: 4px; 
            font-weight: bold; 
            font-size: 1.2rem; 
            color: #3498db; 
            flex-shrink: 0; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
        }
        
        .grid-container.layout-horiz { 
            display: flex; 
            flex-direction: column; 
            gap: 10px; 
            height: 100%; 
        }
        
        .column.layout-horiz { 
            display: flex; 
            flex-direction: row; 
            gap: 10px; 
            flex: 1 1 0; 
            overflow: hidden; 
            width: 100%; 
        }
        
        .col-title.layout-horiz { 
            padding: 0 10px; 
            writing-mode: vertical-rl; 
            transform: rotate(180deg); 
            min-width: 40px; 
        }
        
        .img-stack.layout-horiz { 
            display: flex; 
            flex-direction: row; 
            gap: 8px; 
            flex-grow: 1; 
            min-width: 0; 
            overflow: hidden; 
            justify-content: center; 
        }
        
        .grid-container.layout-vert { 
            display: grid; 
            gap: 10px; 
            height: 100%; 
        }
        
        .column.layout-vert { 
            display: flex; 
            flex-direction: column; 
            gap: 10px; 
            overflow: hidden; 
        }
        
        .col-title.layout-vert { 
            padding: 10px 0; 
            min-height: 30px; 
            writing-mode: horizontal-tb; 
            transform: none; 
        }
        
        .img-stack.layout-vert { 
            display: flex; 
            flex-direction: column; 
            gap: 8px; 
            flex-grow: 1; 
            min-height: 0; 
            overflow: hidden; 
            justify-content: center; 
        }

        .img-wrapper { 
            position: relative; 
            flex: 1 1 0; 
            min-height: 0; 
            min-width: 0; 
            border-radius: 4px; 
            overflow: hidden; 
            background: #1a1a1a; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            box-sizing: border-box; 
            border: 4px solid transparent; 
            transition: border-color 0.3s; 
            cursor: zoom-in; 
        }
        
        .img-wrapper img { 
            width: 100%; 
            height: 100%; 
            object-fit: contain; 
            object-position: center; 
            display: block; 
        }
        
        .overlay { 
            position: absolute; 
            bottom: 0; 
            left: 0; 
            right: 0; 
            background: rgba(0,0,0,0.8); 
            display: flex; 
            justify-content: space-between; 
            padding: 4px 8px; 
            font-size: 14px; 
            font-weight: bold; 
            pointer-events: none; 
        }
        
        .overlay-top { 
            position: absolute; 
            top: 0; 
            left: 0; 
            right: 0; 
            background: rgba(0,0,0,0.8); 
            text-align: center; 
            padding: 4px; 
            font-size: 14px; 
            font-weight: bold; 
            color: #f1c40f; 
            pointer-events: none; 
        }
        
        .status-msg { 
            text-align: center; 
            color: #e74c3c; 
            margin-top: 20px; 
            width: 100%; 
            font-size: 0.9rem;
        }
        
        #zoom-container { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            z-index: 9999; 
            justify-content: center; 
            align-items: center; 
            cursor: zoom-out; 
            background: rgba(0, 0, 0, 0.9); 
        }
        
        #zoom-container.visible { 
            display: flex; 
        }
    </style>
</head>
<body>
    <div id="app-wrapper" class="rot-0">
        <div class="header">
            <div class="header-left">
                <h1 id="main-title">{{ t('VISÃO GLOBAL') }}</h1>
                <div id="cycle-status" style="color: #bdc3c7; font-size: 14px; margin-top: 5px;"></div>
            </div>
            <div class="header-right">
                <div style="font-size: 11px; color: #bdc3c7; margin-bottom: 2px; font-weight: bold; text-transform: uppercase;">
                    {{ t('Legenda de Câmaras de Rejeição') }}
                </div>
                {% if view_type == 'fundo' %}
                <div class="legend-container">
                    {% for cam_id, cam_info in fundo_cameras.items() %}
                        <div class="legend-item">
                            <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                            {{ cam_info.nome }} ({{ cam_id }})
                        </div>
                    {% endfor %}
                </div>
                {% else %}
                <div style="display: flex; border-radius: 6px; overflow: hidden; border: 1px solid #34495e; margin-top: 2px;">
                    <div style="background-color: #ecf0f1; padding: 4px; display: flex; flex-direction: column; gap: 4px;">
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_normal_top.items() %}
                                <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid {{ cam_info.cor }};">
                                    <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                    {{ cam_id }}
                                </div>
                            {% endfor %}
                        </div>
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_normal_bottom.items() %}
                                <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid {{ cam_info.cor }};">
                                    <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                    {{ cam_id }}
                                </div>
                            {% endfor %}
                            <div class="legend-item" style="background: #fff; color: #2c3e50; border: 1px solid #bdc3c7; border-bottom: 3px solid #95a5a6;">
                                <div class="legend-color" style="background-color: #95a5a6;"></div>
                                {{ t('Indef.') }}
                            </div>
                        </div>
                    </div>
                    <div style="background-color: #111; padding: 4px; display: flex; flex-direction: column; gap: 4px; border-left: 2px solid #34495e;">
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index <= 2 %}
                                    <div class="legend-item" style="background: #2c3e50; border: 1px solid #111; border-bottom: 3px solid {{ cam_info.cor }};">
                                        <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                        {{ cam_id }}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                        <div style="display: flex; gap: 4px; justify-content: center;">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index > 2 %}
                                    <div class="legend-item" style="background: #2c3e50; border: 1px solid #111; border-bottom: 3px solid {{ cam_info.cor }};">
                                        <div class="legend-color" style="background-color: {{ cam_info.cor }};"></div>
                                        {{ cam_id }}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                    </div>
                </div>
                {% endif %}
            </div>
        </div>
        <div id="overview-grid"></div>
        <div id="zoom-container"></div>
    </div>

    <script>
        const currentView = '{{ view_type }}';
        const ZOOM_PERCENTAGE = {{ zoom_percentage }};
        const fundoCameras = {{ fundo_cameras | tojson | safe if fundo_cameras else '{}' }};
        const lateralCameras = {{ lateral_cameras_all | tojson | safe if lateral_cameras_all else '{}' }};
        const isFundo = currentView === 'fundo';
        const isLateral = currentView === 'lateral';
        
        function fetchOverview() {
            if (document.body.classList.contains('zoomed-active')) return;
            document.getElementById('main-title').innerText = currentView === 'lateral' ? "{{ t('VISÃO GLOBAL - LATERAIS') }}" : "{{ t('VISÃO GLOBAL - FUNDOS') }}";
            
            fetch(`/api/overview_data?view=${currentView}`)
                .then(r => r.json())
                .then(data => {
                    const appWrapper = document.getElementById('app-wrapper');
                    appWrapper.className = 'rot-' + data.orientation;

                    const layoutMode = data.layout === 'vertical' ? 'layout-vert' : 'layout-horiz';
                    
                    const grid = document.getElementById('overview-grid');
                    grid.className = 'grid-container ' + layoutMode;
                    grid.innerHTML = '';
                    
                    if (layoutMode === 'layout-vert') {
                        grid.style.gridTemplateColumns = `repeat(${data.columns.length || 1}, 1fr)`;
                    } else {
                        grid.style.gridTemplateColumns = '';
                    }
                    
                    data.columns.forEach(col => {
                        const colDiv = document.createElement('div');
                        colDiv.className = 'column ' + layoutMode;
                        
                        const titleDiv = document.createElement('div');
                        titleDiv.className = 'col-title ' + layoutMode;
                        titleDiv.innerText = col.title;
                        colDiv.appendChild(titleDiv);
                        
                        const stackDiv = document.createElement('div');
                        stackDiv.className = 'img-stack ' + layoutMode;
                        
                        if (col.images.length === 0) {
                            stackDiv.innerHTML = `<div class="status-msg">{{ t('Sem imagens') }}</div>`;
                        } else {
                            col.images.forEach(imgData => {
                                const wrapper = document.createElement('div');
                                wrapper.className = 'img-wrapper';
                                
                                let cId = imgData.cam_id ? imgData.cam_id.toString().trim() : 'indefinido';
                                let borderCol = '#333';
                                if (isFundo && fundoCameras[cId]) {
                                    borderCol = fundoCameras[cId].cor;
                                } else if (isLateral && lateralCameras[cId]) {
                                    borderCol = lateralCameras[cId].cor;
                                }
                                wrapper.style.border = `4px solid ${borderCol}`;
                                
                                const img = document.createElement('img');
                                img.src = imgData.url;
                                wrapper.appendChild(img);
                                
                                if (imgData.overlay_top) {
                                    const t = document.createElement('div');
                                    t.className = 'overlay-top';
                                    t.innerText = imgData.overlay_top;
                                    wrapper.appendChild(t);
                                }
                                
                                const b = document.createElement('div');
                                b.className = 'overlay';
                                
                                const bl = document.createElement('span');
                                bl.innerText = imgData.overlay_bottom_left || '';
                                const br = document.createElement('span');
                                br.innerText = imgData.overlay_bottom_right || '';
                                
                                b.appendChild(bl);
                                b.appendChild(br);
                                wrapper.appendChild(b);
                                
                                wrapper.addEventListener('dblclick', (event) => {
                                    const zoomContainer = document.getElementById('zoom-container');
                                    zoomContainer.innerHTML = '';
                                    
                                    const zoomWrapper = document.createElement('div');
                                    zoomWrapper.style.position = 'relative';
                                    zoomWrapper.style.width = `300px`; 
                                    zoomWrapper.style.borderRadius = '8px';
                                    zoomWrapper.style.overflow = 'hidden';
                                    zoomWrapper.style.boxShadow = '0 10px 50px rgba(0,0,0,0.9)';
                                    zoomWrapper.style.backgroundColor = '#34495e';
                                    zoomWrapper.style.transform = `scale(${ZOOM_PERCENTAGE / 100})`;
                                    zoomWrapper.style.transformOrigin = 'center center';
                                    zoomWrapper.style.border = `8px solid ${borderCol}`;
                                    zoomWrapper.style.boxSizing = 'border-box';
                                    
                                    const zoomedImg = document.createElement('img');
                                    zoomedImg.src = imgData.url;
                                    zoomedImg.style.width = '100%';
                                    zoomedImg.style.display = 'block';
                                    zoomWrapper.appendChild(zoomedImg);
                                    
                                    if (imgData.overlay_top) {
                                        const t = document.createElement('div');
                                        t.className = 'overlay-top';
                                        t.textContent = imgData.overlay_top;
                                        zoomWrapper.appendChild(t);
                                    }
                                    
                                    const o = document.createElement('div');
                                    o.className = 'overlay';
                                    
                                    const ob = document.createElement('div');
                                    ob.className = 'overlay-bottom';
                                    
                                    const spanBl = document.createElement('span');
                                    spanBl.textContent = imgData.overlay_bottom_left || '';
                                    
                                    const spanBr = document.createElement('span');
                                    spanBr.textContent = imgData.overlay_bottom_right || '';
                                    
                                    ob.appendChild(spanBl);
                                    ob.appendChild(spanBr);
                                    o.appendChild(ob);
                                    zoomWrapper.appendChild(o);
                                    
                                    zoomContainer.appendChild(zoomWrapper);
                                    zoomContainer.classList.add('visible');
                                    document.body.classList.add('zoomed-active');
                                });

                                stackDiv.appendChild(wrapper);
                            });
                        }
                        colDiv.appendChild(stackDiv);
                        grid.appendChild(colDiv);
                    });
                })
                .catch(err => console.error("Erro ao carregar overview:", err));
        }

        function closeZoom() {
            const zoomContainer = document.getElementById('zoom-container');
            zoomContainer.classList.remove('visible');
            document.body.classList.remove('zoomed-active');
            zoomContainer.innerHTML = '';
        }

        function checkForCycle() {
            fetch('/api/cycle_info')
                .then(response => response.json())
                .then(data => {
                    if (data.cycle_active) {
                        const statusElem = document.getElementById('cycle-status');
                        if (statusElem) {
                            statusElem.innerHTML = `<strong>{{ t('Modo Ciclo Ativo.') }}</strong> {{ t('Próxima:') }} ${data.next_machine}`;
                        }
                        setTimeout(function attemptRedirect() {
                            if (!document.body.classList.contains('zoomed-active')) {
                                window.location.href = `http://${window.location.hostname}:${data.next_port}`;
                            } else {
                                setTimeout(attemptRedirect, 2000); 
                            }
                        }, data.cycle_interval_sec * 1000);
                    } else {
                        setTimeout(checkForCycle, 10000); 
                    }
                })
                .catch(err => {
                    console.error('Could not fetch cycle info:', err);
                    setTimeout(checkForCycle, 10000); 
                });
        }

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('zoom-container').addEventListener('click', closeZoom);
            fetchOverview();
            
            const refreshInterval = {{ refresh_interval * 1000 }};
            const finalInterval = Math.max(10000, refreshInterval); 
            setInterval(fetchOverview, finalInterval); 
            
            checkForCycle();
        });
    </script>
</body>
</html>
"""

# ==============================================================================
# LÓGICA CENTRAL DE EXTRAÇÃO DE IMAGENS BLINDADA
# ==============================================================================
def fetch_images_data(linha_str, maquina_str, limit=50, selected_cams=None):
    try:
        current_main_config = load_json_config(CONFIG_FILE)

        linhas_cfg = current_main_config.get('linhas')
        if not isinstance(linhas_cfg, dict): linhas_cfg = {}
        
        linha_cfg = linhas_cfg.get(linha_str)
        if not isinstance(linha_cfg, dict): linha_cfg = {}
        
        maquina_cfg = linha_cfg.get(maquina_str)
        if not isinstance(maquina_cfg, dict): maquina_cfg = {}
        
        current_source_path = get_active_dst_path(current_main_config, linha_str, maquina_str)
        
        if not current_source_path or not isinstance(current_source_path, str):
             return []

        merged_cfg = get_merged_mosaic_config(linha_str, maquina_str)
        xml_conf = merged_cfg["xml_config"]
        overlay_conf = merged_cfg["overlay_config"]
        filter_conf_main = merged_cfg["filter_config"]
        
        if selected_cams is None:
            if 'lateral' in maquina_str.lower():
                selected_num_cam_values = set()
            else:
                filter_conf = filter_conf_main.get("NUM_CAM")
                if not isinstance(filter_conf, dict): filter_conf = {}
                
                enabled = filter_conf.get("enabled", False)
                selected_num_cam_values = set(filter_conf.get("selected_values") or []) if enabled else set()
        else:
            selected_num_cam_values = selected_cams
            
        is_filter_active = bool(selected_num_cam_values)
        limit = safe_int(limit, 50)
        
        all_jpgs = []
        path_generator = generate_search_paths(current_source_path, current_main_config)

        for path in path_generator:
            if os.path.exists(path):
                all_jpgs.extend(get_cached_dir(path).get('jpgs', []))
            if limit > 0 and len(all_jpgs) > limit * 3:
                break
                
        if not all_jpgs:
            return []

        all_jpgs.sort(key=lambda x: x["mtime"], reverse=True)
        
        images_data = []
        field_to_alias = xml_conf.get("selectedFields")
        if not isinstance(field_to_alias, dict): field_to_alias = {}

        for img_obj in all_jpgs:
            if limit > 0 and len(images_data) >= limit:
                break

            jpg_path = img_obj["path"]
            
            xml_path_lower = os.path.splitext(jpg_path)[0] + '.xml'
            xml_path_upper = os.path.splitext(jpg_path)[0] + '.XML'
            xml_path = xml_path_upper if os.path.exists(xml_path_upper) else xml_path_lower
            
            xml_data = {}
            num_cam = "indefinido"
            
            if xml_path in _xml_cache and _xml_cache[xml_path]['mtime'] == img_obj["mtime"]:
                xml_data = _xml_cache[xml_path]['data']
            elif os.path.exists(xml_path):
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    
                    for child in root:
                        xml_data[str(child.tag)] = str(child.text).strip() if child.text else ""
                    
                    if len(_xml_cache) > 20000:
                        _xml_cache.clear()
                    _xml_cache[xml_path] = {'mtime': img_obj["mtime"], 'data': xml_data}
                except Exception:
                    pass
            
            num_cam = xml_data.get("NUM_CAM", "indefinido")
            
            if is_filter_active:
                if num_cam not in selected_num_cam_values:
                    continue

            final_xml_data = {}
            for field, alias in field_to_alias.items():
                val_translated = _t(xml_data.get(field, "N/A"))
                final_xml_data[alias] = val_translated
            
            br_key = overlay_conf.get("bottom_right")
            overlay_br_val = ""

            if br_key == '_file_timestamp_':
                try:
                    mtime = img_obj["mtime"]
                    overlay_br_val = datetime.fromtimestamp(mtime).strftime('%H:%M')
                except Exception:
                    overlay_br_val = "HH:MM?"
            elif br_key and isinstance(br_key, str):
                br_val_from_xml = xml_data.get(br_key)
                if br_val_from_xml:
                    overlay_br_val = str(br_val_from_xml)
                    try:
                        dt_object = None
                        value_to_parse = overlay_br_val.split('.')[0].replace('T', ' ')
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y%m%d%H:%M:%S"]:
                            try:
                                dt_object = datetime.strptime(value_to_parse, fmt)
                                break
                            except ValueError:
                                continue
                        if dt_object:
                            overlay_br_val = dt_object.strftime('%H:%M')
                    except Exception:
                        pass

            top_tag = overlay_conf.get("top")
            top_val = xml_data.get(top_tag) if top_tag else ""
            
            bl_tag = overlay_conf.get("bottom_left")
            bl_val = xml_data.get(bl_tag) if bl_tag else ""

            relative_path = os.path.relpath(jpg_path, current_source_path)
            images_data.append({
                "url": f"/image/{linha_str}/{maquina_str}/{relative_path.replace(os.sep, '/')}", 
                "overlay_top": _t(str(top_val)) if top_val else "",
                "overlay_bottom_left": _t(str(bl_val)) if bl_val else "",
                "overlay_bottom_right": overlay_br_val,
                "cam_id": num_cam
            })
            
        return images_data
    except Exception as e:
        logging.error(f"Falha segura em fetch_images_data: {e}")
        return []

@app.route('/')
def mosaic_page():
    if LINHA == "Global":
        return redirect(url_for('overview_page'))

    merged_cfg = get_merged_mosaic_config(LINHA, MAQUINA)
    display_conf = merged_cfg["display_config"]
    
    img_size = safe_int(display_conf.get("image_size"), 250)
    grid_columns = safe_int(display_conf.get("grid_columns"), 4)
    grid_lines = safe_int(display_conf.get("grid_lines"), 3)
    ref_interval = safe_int(display_conf.get("refresh_interval"), 30)
    z_pct = safe_int(display_conf.get("zoom_percentage"), 250)
    rotation = safe_int(display_conf.get("orientation", 0), 0)
    
    current_main_config = load_json_config(CONFIG_FILE)
    maquinas_list = []
    
    linhas_cfg = current_main_config.get('linhas')
    if not isinstance(linhas_cfg, dict): linhas_cfg = {}
    
    linha_config = linhas_cfg.get(LINHA)
    if not isinstance(linha_config, dict): linha_config = {}
    
    for m_name, m_conf in linha_config.items():
        if LINHA == "34" and m_name in ["1", "2"]:
            continue
            
        if isinstance(m_conf, dict) and 'dst' in m_conf:
            maquinas_list.append(m_name)
            
    is_fundo = 'fundo' in MAQUINA.lower()
    is_lateral = 'lateral' in MAQUINA.lower()
    fundo_cameras, lateral_normal_top, lateral_normal_bottom, lateral_stress, lateral_cameras_all = get_camera_dicts()
    
    html_content = render_template_string(
        MOSAIC_TEMPLATE,
        linha=LINHA,
        maquina=MAQUINA,
        port=SERVER_PORT,
        image_size=img_size,
        grid_columns=grid_columns,
        grid_lines=grid_lines,
        refresh_interval=ref_interval,
        zoom_percentage=z_pct,
        rotation=rotation,
        maquinas=maquinas_list,
        is_fundo=is_fundo,
        is_lateral=is_lateral,
        fundo_cameras=fundo_cameras,
        lateral_normal_top=lateral_normal_top,
        lateral_normal_bottom=lateral_normal_bottom,
        lateral_stress=lateral_stress,
        lateral_cameras_all=lateral_cameras_all
    )
    
    response = make_response(html_content)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/cycle_info')
def get_cycle_info():
    try:
        main_cfg = load_json_config(CONFIG_FILE)
        if not isinstance(main_cfg, dict): return jsonify({"cycle_active": False})

        if LINHA == "Global":
            vg_cfg = main_cfg.get('visao_global', {})
            if not vg_cfg.get('cycle_mode_active'):
                return jsonify({"cycle_active": False})
                
            cycle_interval = safe_int(vg_cfg.get('cycle_time_sec'), 30)
            
            active_mosaic_machines = []
            if vg_cfg.get('mosaic_lateral_active') and vg_cfg.get('port_lateral'):
                active_mosaic_machines.append({"name": "lateral", "port": vg_cfg['port_lateral']})
            if vg_cfg.get('mosaic_fundo_active') and vg_cfg.get('port_fundo'):
                active_mosaic_machines.append({"name": "fundo", "port": vg_cfg['port_fundo']})
                
            active_mosaic_machines.sort(key=lambda x: x['name'])
        else:
            linhas_cfg = main_cfg.get('linhas')
            if not isinstance(linhas_cfg, dict): return jsonify({"cycle_active": False})
            
            linha_config = linhas_cfg.get(LINHA)
            if not isinstance(linha_config, dict): return jsonify({"cycle_active": False})

            if not linha_config.get('cycle_mode_active'):
                return jsonify({"cycle_active": False}) 
            
            cycle_interval = safe_int(linha_config.get('cycle_time_sec'), 30)

            active_mosaic_machines = []
            for machine_name, machine_cfg in linha_config.items():
                if LINHA == "34" and machine_name in ["1", "2"]:
                    continue
                    
                if isinstance(machine_cfg, dict) and machine_cfg.get('mosaic_active') and machine_cfg.get('mosaic_port'):
                    active_mosaic_machines.append({
                        "name": machine_name,
                        "port": machine_cfg['mosaic_port']
                    })
        
            active_mosaic_machines.sort(key=lambda x: x['name'])

        if len(active_mosaic_machines) < 2:
            return jsonify({"cycle_active": False})

        current_machine_index = -1
        for i, machine in enumerate(active_mosaic_machines):
            if machine['name'] == MAQUINA:
                current_machine_index = i
                break
        
        if current_machine_index == -1:
            return jsonify({"cycle_active": False})

        next_machine_index = (current_machine_index + 1) % len(active_mosaic_machines)
        next_machine = active_mosaic_machines[next_machine_index]
        
        return jsonify({
            "cycle_active": True,
            "cycle_interval_sec": cycle_interval,
            "next_port": next_machine['port'],
            "current_machine": MAQUINA,
            "next_machine": next_machine['name']
        })
    except Exception as e:
        return jsonify({"cycle_active": False, "error": str(e)})

@app.route('/api/images')
def get_images():
    try:
        merged_cfg = get_merged_mosaic_config(LINHA, MAQUINA)
        display_conf = merged_cfg["display_config"]
        
        limit = safe_int(display_conf.get("max_images"), 50)
        rotation = safe_int(display_conf.get("orientation", 0), 0)
        grid_cols = safe_int(display_conf.get("grid_columns"), 4)
        img_size = safe_int(display_conf.get("image_size"), 300)
        
        data = fetch_images_data(LINHA, MAQUINA, limit=limit)
        return jsonify({
            "images": data,
            "orientation": rotation,
            "grid_columns": grid_cols,
            "image_size": img_size
        })
    except Exception as e:
        return jsonify({"error": str(e), "images": []}), 500

@app.route('/image/<req_linha>/<req_maquina>/<path:filepath>')
def serve_image(req_linha, req_maquina, filepath):
    try:
        current_main_config = load_json_config(CONFIG_FILE)
        if not isinstance(current_main_config, dict): return abort(404)
        
        base_image_path = get_active_dst_path(current_main_config, req_linha, req_maquina)
        
        if not base_image_path or not isinstance(base_image_path, str): 
            return abort(404) 
        
        full_path_to_file = os.path.join(base_image_path, filepath)
        
        if not os.path.normpath(full_path_to_file).startswith(os.path.normpath(base_image_path)):
            return abort(403)

        if not os.path.exists(full_path_to_file):
            return abort(404) 
            
        directory = os.path.dirname(full_path_to_file) 
        filename = os.path.basename(full_path_to_file)
        return send_from_directory(directory, filename)
    except Exception:
        return abort(404)

@app.route('/api/overview_data')
def overview_data():
    try:
        view_type = request.args.get('view', MAQUINA.lower())
        if view_type not in ['lateral', 'fundo']:
            view_type = 'lateral'

        mosaic_cfg = load_json_config(MOSAIC_CONFIG_FILE)
        overview_cfg = mosaic_cfg.get('overview', {}).get(view_type, {})
        
        layout = overview_cfg.get('layout', 'horizontal')
        orientation = safe_int(overview_cfg.get('orientation', 0), 0)
        max_imgs = safe_int(overview_cfg.get('max_images'), 8)
        active_machines = overview_cfg.get('active_machines')
        
        columns_def = []
        if isinstance(active_machines, dict) and active_machines:
            for l_str, m_list in active_machines.items():
                for m_str in m_list:
                    columns_def.append((l_str, m_str, f"{_t('Linha')} {l_str} - {_t(m_str.capitalize())}"))
        else:
            if view_type == 'lateral':
                columns_def = [
                    ('21', 'lateral', f"{_t('Linha')} 21"), ('22', 'lateral', f"{_t('Linha')} 22"), ('23', 'lateral', f"{_t('Linha')} 23"),
                    ('24', 'lateral', f"{_t('Linha')} 24"), ('31', 'lateral', f"{_t('Linha')} 31"), ('32', 'lateral', f"{_t('Linha')} 32"),
                    ('33', 'lateral', f"{_t('Linha')} 33"), ('34', 'lateral1', 'L34 - L1'), ('34', 'lateral2', 'L34 - L2')
                ]
            else:
                columns_def = [
                    ('21', 'fundo', f"{_t('Linha')} 21"), ('22', 'fundo', f"{_t('Linha')} 22"), ('23', 'fundo', f"{_t('Linha')} 23"),
                    ('24', 'fundo', f"{_t('Linha')} 24"), ('31', 'fundo', f"{_t('Linha')} 31"), ('32', 'fundo', f"{_t('Linha')} 32"),
                    ('33', 'fundo', f"{_t('Linha')} 33"), ('34', 'fundo1', 'L34 - F1'), ('34', 'fundo2', 'L34 - F2')
                ]
            
        results = []
        for linha_str, maquina_str, title in columns_def:
            imgs = fetch_images_data(linha_str, maquina_str, limit=max_imgs)
            results.append({
                "title": title,
                "images": imgs
            })
            
        return jsonify({
            "view": view_type, 
            "columns": results, 
            "layout": layout, 
            "orientation": orientation
        })
    except Exception as e:
        return jsonify({"view": "error", "columns": [], "error": str(e)}), 500

@app.route('/overview')
def overview_page():
    view_type = MAQUINA.lower()
    if view_type not in ['lateral', 'fundo']:
        view_type = 'lateral'

    fundo_cameras, lateral_normal_top, lateral_normal_bottom, lateral_stress, lateral_cameras_all = get_camera_dicts()
    
    merged_cfg = get_merged_mosaic_config(LINHA, MAQUINA)
    z_pct = safe_int(merged_cfg["display_config"].get("zoom_percentage"), 500)
    
    r_interval = safe_int(merged_cfg["display_config"].get("refresh_interval"), 30)
    
    return render_template_string(OVERVIEW_TEMPLATE, 
                                  view_type=view_type,
                                  fundo_cameras=fundo_cameras,
                                  lateral_cameras_all=lateral_cameras_all,
                                  lateral_normal_top=lateral_normal_top,
                                  lateral_normal_bottom=lateral_normal_bottom,
                                  lateral_stress=lateral_stress,
                                  refresh_interval=r_interval,
                                  zoom_percentage=z_pct)

# ==============================================================================
# SECÇÃO DO HISTÓRICO DE MÁQUINA
# ==============================================================================
def get_available_cameras(maquina_nome):
    try:
        main_cfg = load_json_config(CONFIG_FILE)
        if not isinstance(main_cfg, dict): return []
        
        dst_path = get_active_dst_path(main_cfg, LINHA, maquina_nome)
        
        if not dst_path or not isinstance(dst_path, str): return []

        unique_cams = set()
        path_gen = generate_search_paths(dst_path, main_cfg)
        
        for path in path_gen:
            if os.path.exists(path):
                cached_jpgs = get_cached_dir(path).get('jpgs', [])
                for img_obj in cached_jpgs[:100]: 
                    xml_path_lower = os.path.splitext(img_obj["path"])[0] + '.xml'
                    xml_path_upper = os.path.splitext(img_obj["path"])[0] + '.XML'
                    xml_path = xml_path_upper if os.path.exists(xml_path_upper) else xml_path_lower
                    if os.path.exists(xml_path):
                        try:
                            tree = ET.parse(xml_path)
                            cam_elem = tree.getroot().find("NUM_CAM")
                            if cam_elem is not None and cam_elem.text:
                                unique_cams.add(str(cam_elem.text).strip())
                        except Exception: pass
                if len(unique_cams) > 0:
                    return sorted(list(unique_cams))
                        
        return sorted(list(unique_cams))
    except Exception:
        return []

@app.route('/api/historico/data/<req_maquina>')
def historico_data(req_maquina):
    try:
        cams_param = request.args.get('cams', None)
        if cams_param is not None:
            if cams_param == '':
                selected_cams = set()
            else:
                selected_cams = set(cams_param.split(','))
        else:
            selected_cams = None

        current_main_config = load_json_config(CONFIG_FILE)
        if not isinstance(current_main_config, dict): return jsonify({"error": _t("Configuração inválida")}), 500
        
        dst_path = get_active_dst_path(current_main_config, LINHA, req_maquina)

        if not dst_path or not isinstance(dst_path, str): return jsonify({"error": _t("Caminho não encontrado")}), 404

        curr_shift, curr_start_dt, curr_end_dt, prev_shift, prev_start_dt, prev_end_dt = get_current_and_prev_shift_ranges(current_main_config)
        
        path_curr = os.path.join(dst_path, curr_start_dt.strftime("%Y-%m-%d"), curr_shift)
        path_prev = os.path.join(dst_path, prev_start_dt.strftime("%Y-%m-%d"), prev_shift)
        
        all_jpgs = []
        for path in [path_prev, path_curr]:
            if os.path.exists(path):
                all_jpgs.extend(get_cached_dir(path).get('jpgs', []))

        all_jpgs.sort(key=lambda x: x["mtime"], reverse=True)
        
        all_jpgs = all_jpgs[:1500] 
        
        images_data = []
        counts = {}
        
        for img_obj in all_jpgs:
            jpg_path = img_obj["path"]
            xml_path_lower = os.path.splitext(jpg_path)[0] + '.xml'
            xml_path_upper = os.path.splitext(jpg_path)[0] + '.XML'
            xml_path = xml_path_upper if os.path.exists(xml_path_upper) else xml_path_lower
            
            xml_data = {}
            num_cam = "indefinido"

            if xml_path in _xml_cache and _xml_cache[xml_path]['mtime'] == img_obj["mtime"]:
                xml_data = _xml_cache[xml_path]['data']
            elif os.path.exists(xml_path):
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    
                    for child in root:
                        xml_data[str(child.tag)] = str(child.text).strip() if child.text else ""
                    
                    if len(_xml_cache) > 20000:
                        _xml_cache.clear()
                    _xml_cache[xml_path] = {'mtime': img_obj["mtime"], 'data': xml_data}
                except Exception:
                    pass
                    
            extracted_cam = xml_data.get("NUM_CAM", "indefinido")
            if str(extracted_cam).strip() != "":
                num_cam = str(extracted_cam).strip()

            counts[num_cam] = counts.get(num_cam, 0) + 1

            if selected_cams is not None and num_cam not in selected_cams:
                continue
                
            mtime = img_obj["mtime"]
            dt = datetime.fromtimestamp(mtime)
            relative_path = os.path.relpath(jpg_path, dst_path)
            
            final_xml_data = {}
            for key, val in xml_data.items():
                tag_translated = _t(key)
                val_translated = _t(val)
                final_xml_data[tag_translated] = val_translated
            
            images_data.append({
                "url": f"/image/{LINHA}/{req_maquina}/{relative_path.replace(os.sep, '/')}",
                "date": dt.strftime('%d/%m/%Y'),
                "time": dt.strftime('%H:%M:%S'),
                "mtime": mtime,
                "cam_id": num_cam,
                "xml": final_xml_data
            })

        return jsonify({"images": images_data, "counts": counts})
    except Exception as e:
        return jsonify({"error": str(e), "images": [], "counts": {}}), 500

@app.route('/historico/<req_maquina>')
def historico_page(req_maquina):
    is_fundo = 'fundo' in req_maquina.lower()
    is_lateral = 'lateral' in req_maquina.lower()
    camaras_disponiveis = []
    
    fundo_cameras, lateral_normal_top, lateral_normal_bottom, lateral_stress, lateral_cameras_all = get_camera_dicts()
    
    if not is_fundo and not is_lateral:
        camaras_disponiveis = get_available_cameras(req_maquina)
    
    merged_cfg = get_merged_mosaic_config(LINHA, req_maquina)
    display_conf = merged_cfg["display_config"]
    rotation = safe_int(display_conf.get("orientation", 0), 0)
    
    main_cfg = load_json_config(CONFIG_FILE)
    curr_shift, curr_start_dt, curr_end_dt, prev_shift, prev_start_dt, prev_end_dt = get_current_and_prev_shift_ranges(main_cfg)
    
    timeline_start_epoch = prev_start_dt.timestamp()
    timeline_divider_epoch = curr_start_dt.timestamp()
    timeline_end_epoch = curr_end_dt.timestamp()
    
    shift_start_str = prev_start_dt.strftime('%d/%m %H:%M')
    shift_end_str = curr_end_dt.strftime('%d/%m %H:%M')
    
    html = """
    <!DOCTYPE html>
    <html lang="{{ lang }}">
    <head>
        <meta charset="UTF-8">
        <title>{{ t('Histórico') }} - {{ t('Linha') }} {{ LINHA }} / {{ t(req_maquina.capitalize()) }}</title>
        <style>
            body, html { 
                font-family: sans-serif; 
                background-color: #1e272e; 
                color: #ecf0f1; 
                margin: 0; 
                padding: 0; 
                width: 100vw; 
                height: 100vh; 
                overflow: hidden; 
            }
            
            #app-wrapper { 
                position: absolute; 
                top: 50%; 
                left: 50%; 
                display: flex; 
                flex-direction: column; 
                box-sizing: border-box; 
                padding: 20px; 
            }
            
            .rot-0 { 
                width: 100vw; 
                height: 100vh; 
                transform: translate(-50%, -50%) rotate(0deg); 
            }
            .rot-90 { 
                width: 100vh; 
                height: 100vw; 
                transform: translate(-50%, -50%) rotate(90deg); 
            }
            .rot-180 { 
                width: 100vw; 
                height: 100vh; 
                transform: translate(-50%, -50%) rotate(180deg); 
            }
            .rot-270 { 
                width: 100vh; 
                height: 100vw; 
                transform: translate(-50%, -50%) rotate(270deg); 
            }

            .header { 
                text-align: center; 
                border-bottom: 2px solid #34495e; 
                padding-bottom: 10px; 
                margin-bottom: 10px; 
                position: relative; 
                flex-shrink: 0; 
            }
            
            h1 { 
                color: #3498db; 
                margin: 0 0 5px 0; 
                font-size: 24px; 
            }
            
            h2 { 
                margin: 0; 
                font-size: 16px; 
                color: #bdc3c7; 
            }
            
            .btn-back { 
                position: absolute; 
                left: 20px; 
                top: 10px; 
                color: #2ecc71; 
                text-decoration: none; 
                font-size: 14px; 
                font-weight: bold; 
                border: 2px solid #2ecc71; 
                padding: 5px 10px; 
                border-radius: 5px; 
                transition: 0.3s; 
            }
            
            .btn-back:hover { 
                background-color: #2ecc71; 
                color: white; 
            }
            
            .filters { 
                background-color: transparent; 
                padding: 0; 
                margin-bottom: 15px; 
                text-align: center; 
                flex-shrink: 0; 
            }
            
            .filters h3 { 
                margin: 0 0 10px 0; 
                color: #f1c40f; 
                font-size: 16px; 
            }
            
            .filters-row { 
                display: flex; 
                flex-wrap: nowrap; 
                width: 100%; 
                gap: 4px; 
                justify-content: center; 
            }
            
            .filter-btn { 
                flex: 1 1 0; 
                min-width: 0; 
                display: flex; 
                flex-direction: column; 
                align-items: center; 
                justify-content: center; 
                padding: 6px 2px; 
                text-align: center; 
                overflow: hidden; 
                border-radius: 4px; 
                border: 1px solid #2c3e50; 
                cursor: pointer; 
                color: #ecf0f1; 
                background: #34495e; 
            }
            
            .filter-btn input[type="checkbox"] { 
                margin: 0 4px 0 0; 
                width: clamp(10px, 1vw, 14px); 
                height: clamp(10px, 1vw, 14px); 
                cursor: pointer; 
                flex-shrink: 0; 
            }
            
            .filter-label-wrapper { 
                display: flex; 
                align-items: center; 
                justify-content: center; 
                width: 100%; 
                overflow: hidden; 
            }
            
            .filter-btn strong { 
                font-size: clamp(0.6rem, 1vw, 0.95rem); 
                white-space: nowrap; 
                text-overflow: ellipsis; 
                overflow: hidden; 
            }
            
            .filter-btn .img-count { 
                font-size: clamp(0.55rem, 0.8vw, 0.85rem); 
                margin-top: 3px; 
                font-weight: bold; 
                white-space: nowrap; 
            }

            .lateral-normal-cb { 
                background: #fff; 
                color: #333; 
                border: 1px solid #ccc; 
            }
            
            .lateral-stress-cb { 
                background: #333; 
                color: #fff; 
                border: 1px solid #555; 
            }
            
            .viewer-container { 
                display: flex; 
                gap: 20px; 
                flex-grow: 1; 
                overflow: hidden; 
            }
            
            .carousel-section { 
                flex: 2; 
                display: flex; 
                flex-direction: column; 
                align-items: center; 
                justify-content: center; 
                background: #2c3e50; 
                padding: 20px; 
                border-radius: 8px; 
                position: relative; 
            }
            
            .image-wrapper { 
                display: flex; 
                align-items: center; 
                justify-content: space-between; 
                width: 100%; 
                height: 100%; 
                min-height: 0; 
            }
            
            .nav-btn { 
                background: #3498db; 
                color: white; 
                border: none; 
                font-size: 30px; 
                padding: 20px 15px; 
                cursor: pointer; 
                border-radius: 8px; 
                transition: 0.3s; 
                outline: none; 
            }
            
            .nav-btn:hover { 
                background: #2980b9; 
                transform: scale(1.05); 
            }
            
            .nav-btn:disabled { 
                background: #7f8c8d; 
                cursor: not-allowed; 
                transform: none; 
            }
            
            .image-container { 
                flex-grow: 1; 
                display: flex; 
                justify-content: center; 
                align-items: center; 
                height: 100%; 
                padding: 0 20px; 
                min-height: 0; 
            }
            
            #hist-img { 
                max-width: 100%; 
                max-height: calc(100vh - 350px); 
                object-fit: contain; 
                border-radius: 4px; 
                box-shadow: 0 4px 15px rgba(0,0,0,0.5); 
                box-sizing: border-box; 
                border: 5px solid transparent; 
                transition: border-color 0.3s; 
            }
            
            .counter { 
                font-size: 20px; 
                font-weight: bold; 
                margin-top: 15px; 
                color: #f1c40f; 
                background: rgba(0,0,0,0.3); 
                padding: 5px 15px; 
                border-radius: 20px; 
            }
            
            /* TIMELINE WIDGET STYLES */
            .timeline-container { 
                position: relative;
                width: 100%; 
                margin-top: 20px; 
                padding: 0 10px; 
                box-sizing: border-box; 
                user-select: none; 
            }
            
            .timeline-labels { 
                display: flex; 
                justify-content: space-between; 
                width: 100%; 
                margin-bottom: 10px; 
                font-size: 0.9rem; 
                color: #bdc3c7; 
                font-weight: bold; 
            }
            
            .timeline-bar { 
                position: relative; 
                height: 40px; 
                background: transparent; 
            }

            .timeline-bg {
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: #1e272e;
                border-radius: 4px;
                border: 1px solid #2c3e50;
                overflow: hidden;
            }
            
            .timeline-colors { 
                position: absolute; 
                top: 0; 
                left: 0; 
                width: 100%; 
                height: 100%; 
                display: block; 
            }
            
            .color-mark { 
                position: absolute; 
                top: 0; 
                height: 100%; 
                width: 2px; 
            }
            
            .timeline-tick {
                position: absolute;
                bottom: 0;
                background-color: rgba(255,255,255,0.7);
                z-index: 1;
            }
            
            .timeline-tick.hour {
                width: 2px;
                height: 15px;
                background-color: #fff;
            }

            .timeline-tick.half-hour {
                width: 1px;
                height: 8px;
            }
            
            .timeline-divider {
                position: absolute;
                top: -5px;
                width: 3px;
                height: 50px;
                background-color: #e74c3c;
                z-index: 2;
                box-shadow: 0 0 5px #e74c3c;
            }

            .divider-label-left {
                position: absolute;
                top: -15px;
                transform: translateX(-110%);
                font-size: 10px;
                color: #e74c3c;
                font-weight: bold;
                white-space: nowrap;
            }

            .divider-label-right {
                position: absolute;
                top: -15px;
                transform: translateX(10%);
                font-size: 10px;
                color: #2ecc71;
                font-weight: bold;
                white-space: nowrap;
            }
            
            .timeline-dim-left, .timeline-dim-right { 
                position: absolute; 
                top: 0; 
                height: 100%; 
                background: rgba(0,0,0,0.75); 
                pointer-events: none; 
                z-index: 3;
            }
            
            .timeline-dim-left { 
                left: 0; 
                width: 0%; 
            }
            
            .timeline-dim-right { 
                right: 0; 
                width: 0%; 
            }
            
            .timeline-labels-container {
                position: relative;
                height: 20px;
                width: 100%;
                margin-top: 2px;
            }

            .timeline-tick-label {
                position: absolute;
                top: 0;
                transform: translateX(-50%);
                font-size: 12px;
                color: #fff;
                font-weight: bold;
                pointer-events: none;
            }

            .timeline-indicator { 
                position: absolute; 
                top: -2px; 
                width: 4px; 
                height: 44px; 
                background: #fff; 
                border-radius: 2px; 
                z-index: 5; 
                transform: translateX(-2px); 
                pointer-events: none; 
                box-shadow: 0 0 5px #fff; 
                display: none; 
            }

            .timeline-handle { 
                position: absolute; 
                top: -5px; 
                width: 12px; 
                height: 50px; 
                background: #ecf0f1; 
                border: 1px solid #7f8c8d; 
                cursor: ew-resize; 
                border-radius: 4px; 
                z-index: 10; 
                transform: translateX(-6px); 
                box-shadow: 0 0 5px rgba(0,0,0,0.5); 
                display: flex;
                justify-content: center;
            }
            
            .handle-tooltip {
                position: absolute;
                top: -25px;
                background: #f1c40f;
                color: #000;
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 11px;
                font-weight: bold;
                pointer-events: none;
                white-space: nowrap;
                display: none;
            }

            .timeline-handle:active .handle-tooltip,
            .timeline-handle:hover .handle-tooltip {
                display: block;
            }

            .data-section { 
                flex: 1; 
                background: #2c3e50; 
                padding: 20px; 
                border-radius: 8px; 
                overflow-y: auto; 
                display: flex; 
                flex-direction: column; 
            }
            
            .data-section h3 { 
                margin-top: 0; 
                color: #3498db; 
                border-bottom: 1px solid #34495e; 
                padding-bottom: 10px; 
            }
            
            .date-time-box { 
                background: #34495e; 
                padding: 15px; 
                border-radius: 4px; 
                margin-bottom: 15px; 
                border-left: 4px solid #f1c40f; 
                display: flex; 
                justify-content: space-between; 
            }
            
            .date-time-box div { 
                font-size: 16px; 
            }
            
            .date-time-box strong { 
                color: #bdc3c7; 
                display: block; 
                font-size: 12px; 
                text-transform: uppercase; 
                margin-bottom: 5px; 
            }
            
            .xml-table { 
                width: 100%; 
                border-collapse: collapse; 
                font-size: 14px; 
            }
            
            .xml-table th, .xml-table td { 
                text-align: left; 
                padding: 8px 10px; 
                border-bottom: 1px solid #34495e; 
            }
            
            .xml-table th { 
                color: #3498db; 
                width: 40%; 
                font-weight: normal; 
            }
            
            .xml-table td { 
                font-weight: bold; 
            }
            
            .loading { 
                display: none; 
                color: #f1c40f; 
                font-weight: bold; 
                font-size: 18px; 
                position: absolute; 
            }
        </style>
    </head>
    <body>
        <div id="app-wrapper" class="rot-{{ rotation }}">
            <div class="header">
                <a href="/" class="btn-back">&larr; {{ t('Voltar') }}</a>
                <h1>{{ t('Histórico de Inspeção') }}</h1>
                <h2>{{ t('Linha') }}: {{ LINHA }} | {{ t('Máquina') }}: {{ t(req_maquina.capitalize()) }}</h2>
            </div>

            <div class="filters">
                {% if is_fundo %}
                <div style="background-color: #2c3e50; padding: 10px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
                    <div class="filters-row">
                        {% for cam_id, cam_info in fundo_cameras.items() %}
                        <label class="filter-btn" style="border-bottom: 4px solid {{ cam_info.cor }};">
                            <div class="filter-label-wrapper">
                                <input type="checkbox" name="camera" value="{{ cam_id }}" checked onchange="applyFilters()"> 
                                <strong>{{ cam_info.nome }}</strong>
                            </div>
                            <span id="count-{{ cam_id }}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                        </label>
                        {% endfor %}
                    </div>
                </div>
                {% elif is_lateral %}
                <div style="display: flex; width: 100%; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
                    <div style="flex: 2; background-color: #ecf0f1; padding: 10px; color: #2c3e50; display: flex; flex-direction: column; justify-content: center;">
                        <div class="filters-row" style="margin-bottom: 4px;">
                            {% for cam_id, cam_info in lateral_normal_top.items() %}
                            <label class="filter-btn lateral-normal-cb" style="border-bottom: 4px solid {{ cam_info.cor }};">
                                <div class="filter-label-wrapper">
                                    <input type="checkbox" name="camera" value="{{ cam_id }}" checked onchange="applyFilters()"> 
                                    <strong>{{ cam_info.nome }}</strong>
                                </div>
                                <span id="count-{{ cam_id }}" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                            </label>
                            {% endfor %}
                            <label class="filter-btn lateral-normal-cb" style="visibility: hidden; pointer-events: none;"></label>
                        </div>
                        <div class="filters-row">
                            {% for cam_id, cam_info in lateral_normal_bottom.items() %}
                            <label class="filter-btn lateral-normal-cb" style="border-bottom: 4px solid {{ cam_info.cor }};">
                                <div class="filter-label-wrapper">
                                    <input type="checkbox" name="camera" value="{{ cam_id }}" checked onchange="applyFilters()"> 
                                    <strong>{{ cam_info.nome }}</strong>
                                </div>
                                <span id="count-{{ cam_id }}" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                            </label>
                            {% endfor %}
                            <label class="filter-btn lateral-normal-cb" style="border-bottom: 4px solid #95a5a6;">
                                <div class="filter-label-wrapper">
                                    <input type="checkbox" name="camera" value="indefinido" checked onchange="applyFilters()"> 
                                    <strong>{{ t('Indef.') }}</strong>
                                </div>
                                <span id="count-indefinido" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                            </label>
                        </div>
                    </div>
                    <div style="flex: 1; background-color: #111; padding: 10px; color: #fff; border-left: 2px solid #34495e; display: flex; flex-direction: column; justify-content: center;">
                        <div class="filters-row" style="margin-bottom: 4px;">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index <= 2 %}
                                <label class="filter-btn lateral-stress-cb" style="border-bottom: 4px solid {{ cam_info.cor }};">
                                    <div class="filter-label-wrapper">
                                        <input type="checkbox" name="camera" value="{{ cam_id }}" checked onchange="applyFilters()"> 
                                        <strong>{{ cam_info.nome }}</strong>
                                    </div>
                                    <span id="count-{{ cam_id }}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                                </label>
                                {% endif %}
                            {% endfor %}
                        </div>
                        <div class="filters-row">
                            {% for cam_id, cam_info in lateral_stress.items() %}
                                {% if loop.index > 2 %}
                                <label class="filter-btn lateral-stress-cb" style="border-bottom: 4px solid {{ cam_info.cor }};">
                                    <div class="filter-label-wrapper">
                                        <input type="checkbox" name="camera" value="{{ cam_id }}" checked onchange="applyFilters()"> 
                                        <strong>{{ cam_info.nome }}</strong>
                                    </div>
                                    <span id="count-{{ cam_id }}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                                </label>
                                {% endif %}
                            {% endfor %}
                        </div>
                    </div>
                </div>
                {% else %}
                <div style="background-color: #2c3e50; padding: 10px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
                    <div class="filters-row" style="flex-wrap: wrap;">
                    {% if cameras %}
                        {% for cam in cameras %}
                        <label class="filter-btn" style="border-bottom: 4px solid #3498db;">
                            <div class="filter-label-wrapper">
                                <input type="checkbox" name="camera" value="{{ cam }}" checked onchange="applyFilters()"> 
                                <strong>{{ t('Câmara') }} {{ cam }}</strong>
                            </div>
                            <span id="count-{{ cam }}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                        </label>
                        {% endfor %}
                    {% else %}
                        <p style="color: #e74c3c; margin: 0; width: 100%;">{{ t('Nenhuma câmara detetada nos registos recentes (XML ausente).') }}</p>
                    {% endif %}
                    </div>
                </div>
                {% endif %}
            </div>

            <div class="viewer-container">
                <div class="carousel-section">
                    <div id="loading-text" class="loading">{{ t('A carregar imagens...') }}</div>
                    <div class="image-wrapper">
                        <button class="nav-btn" id="btn-prev" onclick="prevImg()">&#10094;</button>
                        <div class="image-container">
                            <img id="hist-img" src="" alt="{{ t('Selecione as câmaras para carregar as imagens...') }}" />
                        </div>
                        <button class="nav-btn" id="btn-next" onclick="nextImg()">&#10095;</button>
                    </div>
                    <div class="counter" id="hist-counter">0 / 0</div>
                    
                    <div class="timeline-container">
                        <div class="timeline-labels" style="margin-bottom: 10px;">
                            <span id="label-start">{{ shift_start_str }}</span>
                            <span id="label-current-time" style="color: #3498db; font-size: 16px; font-weight: bold;">--:--:--</span>
                            <span id="label-end">{{ shift_end_str }}</span>
                        </div>
                        <div class="timeline-bar" id="timeline-bar">
                            <div class="timeline-bg">
                                <div class="timeline-colors" id="timeline-colors"></div>
                                <div id="timeline-ticks"></div>
                                <div class="timeline-divider" id="timeline-divider">
                                    <div class="divider-label-left">{{ t('Turno Anterior') }}</div>
                                    <div class="divider-label-right">{{ t('Turno Atual') }}</div>
                                </div>
                                <div class="timeline-dim-left" id="dim-left"></div>
                                <div class="timeline-dim-right" id="dim-right"></div>
                            </div>
                            <div class="timeline-indicator" id="current-indicator"></div>
                            <div class="timeline-handle" id="handle-min"><div class="handle-tooltip" id="tooltip-min">00:00</div></div>
                            <div class="timeline-handle" id="handle-max"><div class="handle-tooltip" id="tooltip-max">00:00</div></div>
                        </div>
                        <div id="timeline-labels-container" class="timeline-labels-container"></div>
                    </div>

                </div>
                
                <div class="data-section">
                    <h3>{{ t('Detalhes da Imagem') }}</h3>
                    <div class="date-time-box">
                        <div><strong>{{ t('Data') }}</strong> <span id="hist-date">--/--/----</span></div>
                        <div><strong>{{ t('Hora') }}</strong> <span id="hist-time">--:--:--</span></div>
                    </div>
                    <div id="hist-xml-data">
                        <p style="color: #bdc3c7; text-align: center; margin-top: 20px;">{{ t('Dados XML aparecerão aqui.') }}</p>
                    </div>
                </div>
            </div>
        </div>

        <script>
            const fundoCameras = {{ fundo_cameras | tojson | safe }};
            const lateralCameras = {{ lateral_cameras_all | tojson | safe }};
            const isFundo = {{ 'true' if is_fundo else 'false' }};
            const isLateral = {{ 'true' if is_lateral else 'false' }};
            const regularCameras = {{ cameras | tojson | safe }};
            
            const timelineStartSec = {{ timeline_start_epoch }};
            const timelineDividerSec = {{ timeline_divider_epoch }};
            const timelineEndSec = {{ timeline_end_epoch }};
            const totalDuration = timelineEndSec - timelineStartSec;
            
            let historyImages = [];
            let filteredImages = [];
            let currentIndex = 0;
            let imageCache = {};
            
            let minPct = 0;
            let maxPct = 100;
            
            function formatSecToTime(sec) {
                let d = new Date(sec * 1000);
                return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
            }
            
            function getOffsetPct(imgEpoch) {
                if (totalDuration <= 0) return 0;
                let pct = ((imgEpoch - timelineStartSec) / totalDuration) * 100;
                return Math.max(0, Math.min(100, pct));
            }
            
            function getColor(cam_id) {
                if (isFundo && fundoCameras[cam_id]) return fundoCameras[cam_id].cor;
                if (isLateral && lateralCameras[cam_id]) return lateralCameras[cam_id].cor;
                if (!isFundo && !isLateral) return '#3498db';
                return '#95a5a6';
            }
            
            function drawTimelineTicks() {
                const ticksContainer = document.getElementById('timeline-ticks');
                const labelsContainer = document.getElementById('timeline-labels-container');
                ticksContainer.innerHTML = '';
                if(labelsContainer) labelsContainer.innerHTML = '';
                
                if (totalDuration <= 0) return;
                
                let step = 1800; // Meia hora
                if (totalDuration > 86400 * 2) step = 3600 * 6;
                
                let startAlign = Math.ceil(timelineStartSec / step) * step;
                
                for(let t = startAlign; t <= timelineEndSec; t += step) {
                    let pct = ((t - timelineStartSec) / totalDuration) * 100;
                    let d = new Date(t * 1000);
                    
                    let isMajor = (step >= 3600) ? true : (d.getMinutes() === 0);
                    
                    let tick = document.createElement('div');
                    tick.className = isMajor ? 'timeline-tick hour' : 'timeline-tick half-hour';
                    tick.style.left = pct + '%';
                    ticksContainer.appendChild(tick);
                    
                    if(isMajor && labelsContainer) {
                        let label = document.createElement('div');
                        label.className = 'timeline-tick-label';
                        label.style.left = pct + '%';
                        let hh = d.getHours().toString().padStart(2, '0');
                        let mm = d.getMinutes().toString().padStart(2, '0');
                        
                        if (hh === '00' && mm === '00') {
                            label.innerText = `${d.getDate()}/${d.getMonth()+1}`;
                        } else {
                            label.innerText = `${hh}:${mm}`;
                        }
                        labelsContainer.appendChild(label);
                    }
                }
                
                let divPct = ((timelineDividerSec - timelineStartSec) / totalDuration) * 100;
                document.getElementById('timeline-divider').style.left = divPct + '%';
            }

            function initTimelineDrag() {
                let isDraggingMin = false;
                let isDraggingMax = false;
                const bar = document.getElementById('timeline-bar');
                const handleMin = document.getElementById('handle-min');
                const handleMax = document.getElementById('handle-max');
                const dimLeft = document.getElementById('dim-left');
                const dimRight = document.getElementById('dim-right');
                const tooltipMin = document.getElementById('tooltip-min');
                const tooltipMax = document.getElementById('tooltip-max');

                handleMin.style.left = '0%';
                handleMax.style.left = '100%';
                dimLeft.style.width = '0%';
                dimRight.style.width = '0%';

                const startDragMin = (e) => { isDraggingMin = true; e.preventDefault(); };
                const startDragMax = (e) => { isDraggingMax = true; e.preventDefault(); };

                handleMin.addEventListener('mousedown', startDragMin);
                handleMax.addEventListener('mousedown', startDragMax);
                handleMin.addEventListener('touchstart', (e) => startDragMin(e.touches[0]), {passive: false});
                handleMax.addEventListener('touchstart', (e) => startDragMax(e.touches[0]), {passive: false});

                const onMove = (e) => {
                    if (!isDraggingMin && !isDraggingMax) return;
                    let clientX = e.clientX || (e.touches && e.touches[0].clientX);
                    if (clientX === undefined) return;
                    
                    let rect = bar.getBoundingClientRect();
                    let x = clientX - rect.left;
                    let pct = (x / rect.width) * 100;
                    pct = Math.max(0, Math.min(100, pct));
                    
                    if (isDraggingMin) {
                        if (pct >= maxPct) pct = maxPct - 1;
                        minPct = pct;
                        handleMin.style.left = minPct + '%';
                        dimLeft.style.width = minPct + '%';
                    } else if (isDraggingMax) {
                        if (pct <= minPct) pct = minPct + 1;
                        maxPct = pct;
                        handleMax.style.left = maxPct + '%';
                        dimRight.style.width = (100 - maxPct) + '%';
                    }
                    
                    let timeMin = timelineStartSec + (minPct / 100) * totalDuration;
                    let timeMax = timelineStartSec + (maxPct / 100) * totalDuration;
                    tooltipMin.innerText = formatSecToTime(timeMin);
                    tooltipMax.innerText = formatSecToTime(timeMax);
                };

                const onEnd = () => {
                    if (isDraggingMin || isDraggingMax) {
                        isDraggingMin = false;
                        isDraggingMax = false;
                        applyFilters();
                    }
                };

                document.addEventListener('mousemove', onMove);
                document.addEventListener('touchmove', onMove, {passive: false});
                document.addEventListener('mouseup', onEnd);
                document.addEventListener('touchend', onEnd);
            }

            function fetchHistoryImages() {
                const selectedCams = Array.from(document.querySelectorAll('input[name="camera"]:checked')).map(cb => cb.value);
                
                document.getElementById('loading-text').style.display = 'block';
                document.getElementById('hist-img').style.opacity = '0.3';
                
                fetch(`/api/historico/data/{{ req_maquina }}?cams=${selectedCams.join(',')}`)
                .then(res => res.json())
                .then(data => {
                    historyImages = data.images || [];
                    
                    if (data.counts) {
                        if (isFundo) {
                            for (const camId in fundoCameras) {
                                const countEl = document.getElementById('count-' + camId);
                                if (countEl) { countEl.innerText = `(${data.counts[camId] || 0} imgs)`; }
                            }
                        } else if (isLateral) {
                            for (const camId in lateralCameras) {
                                const countEl = document.getElementById('count-' + camId);
                                if (countEl) { countEl.innerText = `(${data.counts[camId] || 0} imgs)`; }
                            }
                        } else {
                            for (const cam of regularCameras) {
                                const countEl = document.getElementById('count-' + cam);
                                if (countEl) { countEl.innerText = `(${data.counts[cam] || 0} imgs)`; }
                            }
                        }
                    }
                    
                    document.getElementById('loading-text').style.display = 'none';
                    document.getElementById('hist-img').style.opacity = '1';
                    
                    drawTimelineTicks();
                    applyFilters();
                })
                .catch(err => {
                    console.error(err);
                    document.getElementById('loading-text').innerText = "{{ t('Erro ao carregar.') }}";
                });
            }
            
            function applyFilters() {
                const checkboxes = document.querySelectorAll('input[name="camera"]');
                if (checkboxes.length > 0) {
                    const selectedCams = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
                    historyImages.forEach(img => {
                        img._visible = selectedCams.includes(img.cam_id);
                    });
                } else {
                    historyImages.forEach(img => img._visible = true);
                }
                
                const validHistoryImages = historyImages.filter(img => img._visible);
                
                const colorsContainer = document.getElementById('timeline-colors');
                colorsContainer.innerHTML = '';
                validHistoryImages.forEach(img => {
                    let pct = getOffsetPct(img.mtime);
                    let col = getColor(img.cam_id);
                    let mark = document.createElement('div');
                    mark.className = 'color-mark';
                    mark.style.left = pct + '%';
                    mark.style.backgroundColor = col;
                    colorsContainer.appendChild(mark);
                });
                
                filteredImages = validHistoryImages.filter(img => {
                    let p = getOffsetPct(img.mtime);
                    return p >= minPct && p <= maxPct;
                });
                
                currentIndex = 0;
                renderCurrent();
            }

            function manageImageCache(centerIndex) {
                const radius = 25; 
                const keepSet = new Set();
                for(let i = Math.max(0, centerIndex - radius); i <= Math.min(filteredImages.length - 1, centerIndex + radius); i++) {
                    keepSet.add(i);
                    if(!imageCache[i]) {
                        imageCache[i] = new Image();
                        imageCache[i].src = filteredImages[i].url;
                    }
                }
                for(let k in imageCache) {
                    if(!keepSet.has(parseInt(k))) {
                        delete imageCache[k];
                    }
                }
            }

            function renderCurrent() {
                const imgEl = document.getElementById('hist-img');
                const counterEl = document.getElementById('hist-counter');
                const dateEl = document.getElementById('hist-date');
                const timeEl = document.getElementById('hist-time');
                const xmlEl = document.getElementById('hist-xml-data');
                const btnPrev = document.getElementById('btn-prev');
                const btnNext = document.getElementById('btn-next');
                const indicator = document.getElementById('current-indicator');
                const lblCurrentTime = document.getElementById('label-current-time');

                if (filteredImages.length === 0) {
                    imgEl.src = '';
                    imgEl.alt = "{{ t('Nenhuma imagem encontrada para os filtros selecionados.') }}";
                    imgEl.style.border = '5px solid transparent';
                    counterEl.innerText = '0 / 0';
                    dateEl.innerText = '--/--/----';
                    timeEl.innerText = '--:--:--';
                    lblCurrentTime.innerText = '--:--:--';
                    xmlEl.innerHTML = `<p style="color: #e74c3c; text-align: center;">{{ t('Sem dados para exibir.') }}</p>`;
                    btnPrev.disabled = true;
                    btnNext.disabled = true;
                    indicator.style.display = 'none';
                    return;
                }

                btnPrev.disabled = false;
                btnNext.disabled = false;
                
                manageImageCache(currentIndex);

                const currentData = filteredImages[currentIndex];
                imgEl.src = currentData.url;
                imgEl.alt = "{{ t('Imagem de Inspeção') }}";
                
                let cId = currentData.cam_id ? currentData.cam_id.toString().trim() : 'indefinido';
                
                if (isFundo && fundoCameras[cId]) {
                    imgEl.style.border = '5px solid ' + fundoCameras[cId].cor;
                } else if (isLateral && lateralCameras[cId]) {
                    imgEl.style.border = '5px solid ' + lateralCameras[cId].cor;
                } else {
                    imgEl.style.border = '5px solid transparent';
                }
                
                counterEl.innerText = `${currentIndex + 1} / ${filteredImages.length}`;
                dateEl.innerText = currentData.date;
                timeEl.innerText = currentData.time;
                lblCurrentTime.innerText = currentData.time;
                
                indicator.style.display = 'block';
                indicator.style.left = getOffsetPct(currentData.mtime) + '%';
                
                if (currentData.xml && Object.keys(currentData.xml).length > 0) {
                    let tableHtml = '<table class="xml-table"><tbody>';
                    for (const [key, value] of Object.entries(currentData.xml)) {
                        tableHtml += `<tr><th>${key}</th><td>${value}</td></tr>`;
                    }
                    tableHtml += '</tbody></table>';
                    xmlEl.innerHTML = tableHtml;
                } else {
                    xmlEl.innerHTML = `<p style="color: #e74c3c; text-align: center;">{{ t('Ficheiro XML Inexistente.') }}</p>`;
                }
            }

            // CORREÇÃO: As setas funcionam agora cronologicamente
            // Como a array tem as imagens ordenadas da MAIS RECENTE para a MAIS ANTIGA,
            // Seta Esquerda (frasco anterior no tempo/mais velho) -> Índice sobe
            // Seta Direita (frasco seguinte no tempo/mais novo) -> Índice desce
            function prevImg() {
                if (filteredImages.length === 0) return;
                currentIndex++;
                if (currentIndex >= filteredImages.length) currentIndex = 0;
                renderCurrent();
            }

            function nextImg() {
                if (filteredImages.length === 0) return;
                currentIndex--;
                if (currentIndex < 0) currentIndex = filteredImages.length - 1;
                renderCurrent();
            }

            document.addEventListener('keydown', function(event) {
                if (event.key === 'ArrowLeft') { prevImg(); } 
                else if (event.key === 'ArrowRight') { nextImg(); }
            });

            document.addEventListener('DOMContentLoaded', () => {
                initTimelineDrag();
                fetchHistoryImages();
            });
        </script>
    </body>
    </html>
    """
    
    return render_template_string(
        html,
        LINHA=LINHA,
        req_maquina=req_maquina,
        cameras=camaras_disponiveis,
        is_fundo=is_fundo,
        fundo_cameras=fundo_cameras,
        is_lateral=is_lateral,
        lateral_normal_top=lateral_normal_top,
        lateral_normal_bottom=lateral_normal_bottom,
        lateral_stress=lateral_stress,
        lateral_cameras_all=lateral_cameras_all,
        rotation=rotation,
        timeline_start_epoch=timeline_start_epoch,
        timeline_divider_epoch=timeline_divider_epoch,
        timeline_end_epoch=timeline_end_epoch,
        shift_start_str=shift_start_str,
        shift_end_str=shift_end_str
    )

if __name__ == '__main__': 
    if len(sys.argv) != 4: 
        print("Uso: python mosaic_complete.py <porta> <linha> <maquina>")
        sys.exit(1)

    SERVER_PORT, LINHA, MAQUINA = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    
    os.makedirs(BASE_LOG_PATH, exist_ok=True)
    log_handler = logging.FileHandler(os.path.join(BASE_LOG_PATH, f"mosaic_{LINHA}_{MAQUINA}.log"), encoding='utf-8')
    log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    app.logger.handlers.clear()
    app.logger.addHandler(log_handler)
    logging.getLogger().addHandler(log_handler)
    app.logger.setLevel(logging.INFO)
    
    logging.info(f"Iniciando servidor de mosaico para Linha {LINHA}, Máquina {MAQUINA} na porta {SERVER_PORT}")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, threaded=True, use_reloader=False)