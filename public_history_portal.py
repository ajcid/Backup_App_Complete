#!/usr/bin/env python3
"""
Módulo Independente do Portal de Histórico Público (Consulta Externa)
Executado na porta 5581 pelo processo principal.
Versão Ultra-Otimizada para Redes/NAS com suporte total a Filtros, Cores e Contadores elásticos (Fundo e Lateral).
NOVO: Aba de Artigos colocada temporariamente "Em Desenvolvimento" conforme solicitado.
ATUALIZAÇÃO: Resolução dinâmica de caminhos e i18n com Memória (Cookie). Sem seletores na UI.
CORREÇÃO DE PERFORMANCE: Timeline interativa com horas a bold e tooltips, parse de XML com fallback (.XML/.xml) e RAM Cache para NAS DS423.
INTEGRAÇÃO: Cesto de Imagens, Zoom Inteligente (Principal e Detalhe) e Download ZIP.
UI/UX: Totalmente responsivo (100vh) e contorno de cores adaptado à margem estrita da imagem.
"""

import os
import sys
import json
import re
import xml.etree.ElementTree as ET
import zipfile
import io
from datetime import datetime, timedelta
import logging
import threading
import time
from flask import Flask, render_template_string, request, jsonify, send_from_directory, abort, session, has_request_context, make_response, send_file

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "backup_settings.json")
LOG_FILE = os.path.join(DATA_DIR, "logs", "public_portal.log")
ARTIGOS_DB_FILE = os.path.join(DATA_DIR, "artigos.json")

os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [Portal Público] - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)

app = Flask("PublicPortal")
app.secret_key = os.urandom(24)

# ==============================================================================
# SISTEMA DE INTERNACIONALIZAÇÃO E CACHES GLOBAIS
# ==============================================================================
SUPPORTED_LANGUAGES = ['pt', 'es', 'en', 'pl', 'bg']
DEFAULT_LANG = 'pt'

_translations_cache = {}
_translations_mtime = {}
_hist_xml_cache = {}

_dir_cache = {}
_dir_cache_time = {}
_dir_cache_lock = threading.Lock()

def get_cached_jpgs(path):
    now = time.time()
    with _dir_cache_lock:
        if path in _dir_cache and (now - _dir_cache_time.get(path, 0) < 2.0):
            return _dir_cache[path]
            
    jpgs = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg')):
                    jpgs.append({
                        "path": entry.path,
                        "name": entry.name,
                        "mtime": entry.stat().st_mtime
                    })
    except OSError:
        pass
        
    with _dir_cache_lock:
        _dir_cache[path] = jpgs
        _dir_cache_time[path] = now
        
    return jpgs

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

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Erro ao carregar configuração: {e}")
    return {}

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

# ==============================================================================
# TEMPLATES HTML
# ==============================================================================
EXT_HIST_FILTER_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ t('Portal de Histórico Público') }}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body { 
            font-family: 'Segoe UI', sans-serif; 
            background: #f4f6f9; 
            color: #333; 
            margin: 0; 
            padding: 2vh 2vw; 
            height: 100vh;
            width: 100vw;
            box-sizing: border-box;
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            justify-content: center;
            overflow: hidden;
        }
        
        .header { 
            text-align: center; 
            margin-bottom: 2vh; 
            flex-shrink: 0;
            width: 100%; 
            max-width: 800px; 
        }
        
        .header h1 { 
            color: #2c3e50; 
            font-size: clamp(1.5rem, 3vw, 2.5rem); 
            margin-bottom: 0.5vh; 
        }
        
        .header p { 
            color: #7f8c8d; 
            font-size: clamp(0.9rem, 1.5vw, 1.1rem); 
            margin: 0;
        }
        
        .card { 
            background: white; 
            padding: clamp(1.5rem, 3vh, 2rem) clamp(1.5rem, 3vw, 2rem); 
            border-radius: 12px; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.1); 
            width: 100%; 
            max-width: 800px; 
            flex-grow: 1;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            box-sizing: border-box;
        }
        
        .form-group { 
            margin-bottom: 1.5vh; 
        }
        
        label { 
            display: block; 
            font-weight: bold; 
            color: #34495e; 
            margin-bottom: 0.5vh; 
            font-size: clamp(0.9rem, 1.2vw, 1.1rem); 
            border-bottom: 2px solid #eee; 
            padding-bottom: 0.5vh; 
        }
        
        select, input[type="text"] { 
            width: 100%; 
            padding: clamp(0.5rem, 1vh, 0.8rem); 
            border: 1px solid #ced4da; 
            border-radius: 6px; 
            font-size: clamp(0.9rem, 1vw, 1rem); 
            background: #fff; 
            cursor: pointer; 
            box-sizing: border-box; 
        }
        
        .checkbox-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); 
            gap: 8px; 
            margin-top: 1vh; 
        }
        
        .checkbox-item { 
            background: #f8f9fa; 
            border: 1px solid #dee2e6; 
            border-radius: 6px; 
            padding: 0.6rem; 
            display: flex; 
            align-items: center; 
            gap: 8px; 
            cursor: pointer; 
            transition: 0.2s; 
            font-size: clamp(0.8rem, 1vw, 0.95rem);
        }
        
        .checkbox-item:hover { 
            background: #e9ecef; 
        }
        
        .checkbox-item input { 
            transform: scale(1.1); 
            cursor: pointer; 
            margin: 0;
        }
        
        .btn-submit { 
            width: 100%; 
            padding: clamp(0.8rem, 2vh, 1.2rem); 
            background: #2ecc71; 
            color: white; 
            border: none; 
            border-radius: 8px; 
            font-size: clamp(1rem, 1.5vw, 1.2rem); 
            font-weight: bold; 
            cursor: pointer; 
            transition: 0.3s; 
            margin-top: 2vh; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            gap: 10px; 
            flex-shrink: 0;
        }
        
        .btn-submit:hover { 
            background: #27ae60; 
            transform: translateY(-2px); 
        }
        
        .btn-submit:disabled { 
            background: #95a5a6; 
            cursor: not-allowed; 
            transform: none; 
        }
        
        .loading { 
            display: none; 
            color: #3498db; 
            font-weight: bold; 
            margin-top: 1vh; 
            font-size: 0.9rem;
        }
        
        .tabs { 
            display: flex; 
            width: 100%; 
            border-bottom: 2px solid #ced4da; 
            margin-bottom: 2vh; 
            flex-shrink: 0;
        }
        
        .tab-btn { 
            flex: 1; 
            padding: 1.5vh; 
            background: transparent; 
            border: none; 
            font-size: clamp(0.9rem, 1.2vw, 1.1rem); 
            font-weight: bold; 
            color: #7f8c8d; 
            cursor: pointer; 
            transition: 0.3s; 
            border-bottom: 4px solid transparent; 
        }
        
        .tab-btn:hover { 
            color: #3498db; 
            background: #f0f4ff; 
        }
        
        .tab-btn.active { 
            color: #3498db; 
            border-bottom: 4px solid #3498db; 
        }
        
        .tab-content { 
            display: none; 
            width: 100%; 
            flex-direction: column;
            flex-grow: 1;
            overflow-y: auto; 
            padding-right: 5px;
            animation: fadeIn 0.3s ease-in-out; 
        }
        
        .tab-content.active { 
            display: flex; 
        }
        
        @keyframes fadeIn { 
            from { opacity: 0; } 
            to { opacity: 1; } 
        }

        .art-table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 15px; 
            font-size: clamp(0.8rem, 1vw, 0.95rem); 
        }
        
        .art-table th, .art-table td { 
            padding: 10px; 
            border: 1px solid #ced4da; 
            text-align: center; 
        }
        
        .art-table th { 
            background: #34495e; 
            color: #fff; 
            font-weight: bold; 
        }
        
        .art-table tr:nth-child(even) { 
            background: #f9f9f9; 
        }
        
        .btn-view-art { 
            background: #3498db; 
            color: white; 
            border: none; 
            padding: 6px 12px; 
            border-radius: 4px; 
            cursor: pointer; 
            font-weight: bold; 
            transition: 0.2s; 
        }
        
        .btn-view-art:hover { 
            background: #2980b9; 
        }

        .modal-bg { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(0,0,0,0.8); 
            z-index: 9999; 
            justify-content: center; 
            align-items: center; 
        }
        
        .modal-content-box { 
            background: #fff; 
            padding: 25px; 
            border-radius: 10px; 
            width: 400px; 
            max-width: 90vw; 
            text-align: center; 
        }
        
        .btn-modal { 
            width: 100%; 
            padding: 12px; 
            font-size: 1rem; 
            font-weight: bold; 
            color: #fff; 
            border: none; 
            border-radius: 6px; 
            cursor: pointer; 
            margin-bottom: 10px; 
            transition: 0.2s; 
        }
        
        .btn-modal-lat { background: #3498db; } 
        .btn-modal-lat:hover { background: #2980b9; }
        .btn-modal-fun { background: #2ecc71; } 
        .btn-modal-fun:hover { background: #27ae60; }
        .btn-modal-close { background: #e74c3c; margin-top: 15px; } 
        .btn-modal-close:hover { background: #c0392b; }
        
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #f1f1f1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb { background: #c1c1c1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #a8a8a8; }
    </style>
</head>
<body>
    <div class="header">
        <h1><i class="fas fa-search-location"></i> {{ t('Portal de Consulta Externa') }}</h1>
        <p>{{ t('Selecione os filtros ou procure um artigo para visualizar o histórico de inspeção detalhado.') }}</p>
    </div>
    
    <div class="card">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('tab_data', this)"><i class="fas fa-calendar"></i> {{ t('Busca por Data') }}</button>
            <button class="tab-btn" onclick="switchTab('tab_artigo', this)"><i class="fas fa-box"></i> {{ t('Busca por Artigo') }}</button>
        </div>

        <div id="tab_data" class="tab-content active">
            <div class="form-group">
                <label><i class="fas fa-industry"></i> 1. {{ t('Selecione a Linha') }}</label>
                <select id="sel_linha" onchange="loadMachines()">
                    <option value="">-- {{ t('Escolha uma Linha') }} --</option>
                    {% for l in linhas %}<option value="{{ l }}">{{ l }}</option>{% endfor %}
                </select>
            </div>
            <div class="form-group" id="group_maquina" style="display:none;">
                <label><i class="fas fa-robot"></i> 2. {{ t('Selecione a Máquina') }}</label>
                <select id="sel_maquina" onchange="loadStructure()"></select>
                <div id="loading_struct" class="loading"><i class="fas fa-spinner fa-spin"></i> {{ t('A analisar arquivos na NAS...') }}</div>
            </div>
            <div id="dynamic_filters" style="display:none; flex-direction: column; flex-grow: 1;">
                <div class="form-group">
                    <label><i class="fas fa-calendar"></i> 3. {{ t('Selecione o Ano') }}</label>
                    <select id="sel_year" onchange="renderMonths()"></select>
                </div>
                <div class="form-group">
                    <label><i class="fas fa-calendar-alt"></i> 4. {{ t('Selecione o Mês') }}</label>
                    <select id="sel_month" onchange="renderDays()"></select>
                </div>
                <div class="form-group">
                    <label><i class="fas fa-calendar-day"></i> 5. {{ t('Selecione os Dias (Escolha Múltipla)') }}</label>
                    <div class="checkbox-grid" id="grid_days" onchange="renderShifts()"></div>
                </div>
                <div class="form-group" id="group_shifts" style="display:none;">
                    <label><i class="fas fa-clock"></i> 6. {{ t('Selecione os Turnos (Escolha Múltipla)') }}</label>
                    <div class="checkbox-grid" id="grid_shifts" onchange="validateForm()"></div>
                </div>
                
                <div style="flex-grow: 1;"></div>
                
                <button class="btn-submit" id="btn_submit" onclick="submitSelection()" disabled>
                    <i class="fas fa-images"></i> {{ t('Consultar Imagens Selecionadas') }}
                </button>
            </div>
        </div>

        <div id="tab_artigo" class="tab-content">
            <div style="text-align: center; padding: 3rem; color: #7f8c8d;">
                <i class="fas fa-tools fa-4x" style="margin-bottom: 1.5rem; color: #f39c12;"></i>
                <h2 style="color: #2c3e50; font-size: clamp(1.2rem, 2vw, 1.8rem);">{{ t('Em Desenvolvimento') }}</h2>
                <p style="font-size: clamp(0.9rem, 1.2vw, 1.1rem);">{{ t('A funcionalidade de pesquisa transversal por artigo está atualmente a ser aprimorada e estará disponível brevemente.') }}</p>
            </div>
        </div>
    </div>

    <div id="machineModal" class="modal-bg">
        <div class="modal-content-box">
            <h3><i class="fas fa-camera"></i> {{ t('Qual máquina visualizar?') }}</h3>
            <p id="modal_linha_txt" style="color: #7f8c8d; margin-bottom: 20px; font-size: 0.9rem;"></p>
            <div id="modal_machines_container"></div>
            <button class="btn-modal btn-modal-close" onclick="closeMachineModal()">{{ t('Cancelar') }}</button>
        </div>
    </div>

    <script>
        let dbStructure = {};
        let dbArtigos = {};

        document.addEventListener('DOMContentLoaded', () => {
            fetch('/api/ext_history/artigos')
                .then(r => r.json())
                .then(data => {
                    dbArtigos = data;
                    const datalist = document.getElementById('artigosList');
                    if (datalist) {
                        datalist.innerHTML = '';
                        Object.keys(dbArtigos).sort().forEach(art => {
                            datalist.innerHTML += `<option value="${art}"></option>`;
                        });
                    }
                })
                .catch(err => console.log("{{ t('Base de dados de artigos indisponível ainda.') }}", err));
        });

        function switchTab(tabId, element) {
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            element.classList.add('active');
        }

        function loadMachines() {
            const linha = document.getElementById('sel_linha').value;
            const selMaq = document.getElementById('sel_maquina');
            document.getElementById('group_maquina').style.display = 'none';
            document.getElementById('dynamic_filters').style.display = 'none';
            if(!linha) return;
            selMaq.innerHTML = `<option value="">-- {{ t('A carregar...') }} --</option>`;
            fetch(`/api/ext_history/machines/${linha}`)
                .then(r => r.json())
                .then(data => {
                    selMaq.innerHTML = `<option value="">-- {{ t('Escolha uma Máquina') }} --</option>`;
                    data.machines.forEach(m => selMaq.innerHTML += `<option value="${m}">${m.toUpperCase()}</option>`);
                    document.getElementById('group_maquina').style.display = 'block';
                });
        }
        function loadStructure() {
            const linha = document.getElementById('sel_linha').value;
            const maquina = document.getElementById('sel_maquina').value;
            document.getElementById('dynamic_filters').style.display = 'none';
            if(!maquina) return;
            document.getElementById('loading_struct').style.display = 'block';
            fetch(`/api/ext_history/structure/${linha}/${maquina}`)
                .then(r => r.json())
                .then(data => {
                    dbStructure = data;
                    document.getElementById('loading_struct').style.display = 'none';
                    const selYear = document.getElementById('sel_year');
                    selYear.innerHTML = `<option value="">-- {{ t('Selecione') }} --</option>`;
                    Object.keys(dbStructure).sort().reverse().forEach(y => {
                        selYear.innerHTML += `<option value="${y}">${y}</option>`;
                    });
                    document.getElementById('dynamic_filters').style.display = 'flex';
                });
        }
        function renderMonths() {
            const y = document.getElementById('sel_year').value;
            const selMonth = document.getElementById('sel_month');
            selMonth.innerHTML = `<option value="">-- {{ t('Selecione') }} --</option>`;
            document.getElementById('grid_days').innerHTML = '';
            document.getElementById('group_shifts').style.display = 'none';
            validateForm();
            if(!y) return;
            Object.keys(dbStructure[y]).sort().reverse().forEach(m => {
                selMonth.innerHTML += `<option value="${m}">${m}</option>`;
            });
        }
        function renderDays() {
            const y = document.getElementById('sel_year').value;
            const m = document.getElementById('sel_month').value;
            const gridDays = document.getElementById('grid_days');
            gridDays.innerHTML = '';
            document.getElementById('group_shifts').style.display = 'none';
            validateForm();
            if(!m) return;
            Object.keys(dbStructure[y][m]).sort().forEach(d => {
                gridDays.innerHTML += `<label class="checkbox-item"><input type="checkbox" name="chk_day" value="${d}"> {{ t('Dia') }} ${d}</label>`;
            });
        }
        function renderShifts() {
            const y = document.getElementById('sel_year').value;
            const m = document.getElementById('sel_month').value;
            const selectedDays = Array.from(document.querySelectorAll('input[name="chk_day"]:checked')).map(cb => cb.value);
            const gridShifts = document.getElementById('grid_shifts');
            gridShifts.innerHTML = '';
            if(selectedDays.length === 0) {
                document.getElementById('group_shifts').style.display = 'none';
                validateForm();
                return;
            }
            let availableShifts = new Set();
            selectedDays.forEach(d => { dbStructure[y][m][d].forEach(t => availableShifts.add(t)); });
            Array.from(availableShifts).sort().forEach(t => {
                gridShifts.innerHTML += `<label class="checkbox-item"><input type="checkbox" name="chk_shift" value="${t}" checked> ${t.toUpperCase()}</label>`;
            });
            document.getElementById('group_shifts').style.display = 'block';
            validateForm();
        }
        function validateForm() {
            const selectedDays = document.querySelectorAll('input[name="chk_day"]:checked').length;
            const selectedShifts = document.querySelectorAll('input[name="chk_shift"]:checked').length;
            document.getElementById('btn_submit').disabled = (selectedDays === 0 || selectedShifts === 0);
        }
        function submitSelection() {
            const linha = document.getElementById('sel_linha').value;
            const maquina = document.getElementById('sel_maquina').value;
            const y = document.getElementById('sel_year').value;
            const m = document.getElementById('sel_month').value;
            const days = Array.from(document.querySelectorAll('input[name="chk_day"]:checked')).map(cb => cb.value);
            const shifts = Array.from(document.querySelectorAll('input[name="chk_shift"]:checked')).map(cb => cb.value);
            const selections = days.map(d => ({ date: `${y}-${m}-${d}`, shifts: shifts }));
            const payload = { linha: linha, maquina: maquina, selections: selections };
            sessionStorage.setItem('ext_history_payload', JSON.stringify(payload));
            window.location.href = '/view'; 
        }

        let currentArtigoTarget = null;

        function openMachineModal(encodedInterval) {
            const interval = JSON.parse(decodeURIComponent(encodedInterval));
            currentArtigoTarget = interval;
            
            const linha = interval.linha;
            document.getElementById('modal_linha_txt').innerText = `{{ t('Linha') }} ${linha} | {{ t('Do dia') }} ${interval.inicio} {{ t('até') }} ${interval.fim}`;
            
            const container = document.getElementById('modal_machines_container');
            container.innerHTML = `<p>{{ t('A carregar máquinas disponíveis...') }}</p>`;
            document.getElementById('machineModal').style.display = 'flex';
            
            fetch(`/api/ext_history/machines/${linha}`)
                .then(r => r.json())
                .then(data => {
                    container.innerHTML = '';
                    data.machines.forEach(m => {
                        let btnClass = 'btn-modal';
                        if (m.toLowerCase().includes('lateral')) btnClass += ' btn-modal-lat';
                        else if (m.toLowerCase().includes('fundo')) btnClass += ' btn-modal-fun';
                        else btnClass += ' btn-modal-lat'; 
                        
                        container.innerHTML += `<button class="${btnClass}" onclick="submitArtigoSelection('${m}')"><i class="fas fa-camera"></i> {{ t('Inspeção de') }} ${m.toUpperCase()}</button>`;
                    });
                })
                .catch(err => {
                    container.innerHTML = `<p style="color:red;">{{ t('Erro ao carregar máquinas desta linha.') }}</p>`;
                });
        }

        function closeMachineModal() {
            document.getElementById('machineModal').style.display = 'none';
            currentArtigoTarget = null;
        }

        function getDatesBetween(startDate, endDate) {
            let dates = [];
            let currDate = new Date(startDate);
            let lastDate = new Date(endDate);
            if (currDate.getTime() === lastDate.getTime()) {
                return [startDate];
            }
            while(currDate <= lastDate) {
                dates.push(currDate.toISOString().split('T')[0]);
                currDate.setDate(currDate.getDate() + 1);
            }
            return dates;
        }

        function submitArtigoSelection(maquinaStr) {
            if (!currentArtigoTarget) return;
            const datesToFetch = getDatesBetween(currentArtigoTarget.inicio, currentArtigoTarget.fim);
            const allShifts = ['turno1', 'turno2', 'turno3'];
            const selections = datesToFetch.map(d => ({ date: d, shifts: allShifts }));
            const payload = { 
                linha: currentArtigoTarget.linha, 
                maquina: maquinaStr, 
                selections: selections 
            };
            sessionStorage.setItem('ext_history_payload', JSON.stringify(payload));
            window.location.href = '/view'; 
        }
    </script>
</body>
</html>
"""

EXT_HIST_VIEWER_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ t('Visualizador do Histórico') }}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body { 
            font-family: sans-serif; 
            background-color: #1e272e; 
            color: #ecf0f1; 
            margin: 0; 
            padding: 0; 
            display: flex; 
            flex-direction: column; 
            height: 100vh; 
            width: 100vw;
            overflow: hidden; 
            box-sizing: border-box;
        }
        
        .topbar { 
            background: #2c3e50; 
            padding: 1vh 2vw; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 2px solid #34495e; 
            flex-shrink: 0; 
            height: 8vh;
            box-sizing: border-box;
        }
        
        .btn-back { 
            color: #fff; 
            background: #e74c3c; 
            padding: 0.8vh 1vw; 
            text-decoration: none; 
            border-radius: 4px; 
            font-weight: bold; 
            font-size: clamp(0.8rem, 1.2vw, 1rem);
            white-space: nowrap;
        }
        
        .btn-back:hover {
            background: #c0392b;
        }
        
        .info-title { 
            font-size: clamp(1rem, 1.5vw, 1.2rem); 
            color: #f1c40f; 
            margin: 0; 
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            padding: 0 1vw;
        }
        
        .filters { 
            background-color: transparent; 
            padding: 1vh 2vw; 
            text-align: center; 
            flex-shrink: 0; 
            box-sizing: border-box;
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
            padding: 0.5vh 2px; 
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
            font-size: clamp(0.6rem, 0.9vw, 0.95rem); 
            white-space: nowrap; 
            text-overflow: ellipsis; 
            overflow: hidden; 
        }
        
        .filter-btn .img-count { 
            font-size: clamp(0.55rem, 0.8vw, 0.85rem); 
            margin-top: 2px; 
            font-weight: bold; 
            white-space: nowrap; 
        }

        .lateral-normal-cb { background: #fff; color: #333; border: 1px solid #ccc; }
        .lateral-stress-cb { background: #333; color: #fff; border: 1px solid #555; }
        
        /* ESTRUTURA PRINCIPAL FLEXÍVEL E PROTEGIDA CONTRA OVERFLOW */
        .main-content { 
            flex: 1; 
            display: flex; 
            overflow: hidden; 
            min-height: 0;
            width: 100vw;
        }
        
        .viewer-area { 
            flex: 1; 
            min-width: 0; 
            min-height: 0; 
            position: relative; 
            background: #000; 
            display: flex; 
            flex-direction: column; 
            padding: 10px; 
            box-sizing: border-box;
            overflow: hidden;
        }
        
        .image-wrapper { 
            flex: 1;
            width: 100%; 
            min-height: 0; 
            position: relative;
            overflow: hidden;
        }
        
        .image-container { 
            position: absolute;
            top: 0;
            left: 60px; 
            right: 60px;
            bottom: 0;
            display: flex; 
            justify-content: center; 
            align-items: center; 
            overflow: hidden;
            cursor: zoom-in;
        }
        
        .image-container.zoomed {
            cursor: move;
        }

        .image-container img { 
            max-width: 100%; 
            max-height: 100%; 
            width: auto;
            height: auto;
            border-radius: 4px; 
            box-sizing: border-box; 
            border: 5px solid transparent; 
            transition: border-color 0.3s, transform 0.15s ease-out; 
            transform-origin: center center;
            pointer-events: none;
        }
        
        .nav-zone { 
            position: absolute; 
            top: 0; 
            bottom: 0; 
            width: 60px; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            font-size: clamp(2rem, 4vw, 4rem); 
            color: rgba(255,255,255,0.3); 
            transition: 0.2s; 
            user-select: none; 
            z-index: 10;
        }
        
        .nav-zone:hover { 
            background: rgba(255,255,255,0.1); 
            color: rgba(255,255,255,0.8); 
        }
        
        .nav-left { left: 0; }
        .nav-right { right: 0; }
        
        .data-panel { 
            flex: 0 0 clamp(250px, 25vw, 350px); 
            background: #2c3e50; 
            border-left: 2px solid #34495e; 
            display: flex; 
            flex-direction: column; 
            overflow-y: auto; 
            height: 100%;
        }
        
        .data-header { 
            background: #34495e; 
            padding: 1.5vh 1vw; 
            font-size: clamp(0.9rem, 1.2vw, 1.1rem); 
            text-align: center; 
            color: #3498db; 
            font-weight: bold; 
            position: sticky; 
            top: 0; 
            flex-shrink: 0;
            z-index: 10;
        }
        
        .data-content { 
            padding: 2vh 1vw; 
            display: flex;
            flex-direction: column;
            flex-grow: 1;
        }
        
        .data-box { 
            background: #1e272e; 
            padding: 1.5vh 1vw; 
            border-radius: 6px; 
            margin-bottom: 1.5vh; 
            border-left: 4px solid #f1c40f; 
        }
        
        .data-box span { 
            display: block; 
            color: #bdc3c7; 
            font-size: clamp(0.6rem, 0.8vw, 0.8rem); 
            text-transform: uppercase; 
            margin-bottom: 0.5vh; 
        }
        
        .data-box strong { 
            font-size: clamp(1rem, 1.5vw, 1.2rem); 
            color: #fff; 
        }
        
        .xml-table { 
            width: 100%; 
            border-collapse: collapse; 
            font-size: clamp(0.7rem, 0.9vw, 0.9rem); 
        }
        
        .xml-table th, .xml-table td { 
            text-align: left; 
            padding: 1vh 0.5vw; 
            border-bottom: 1px solid #34495e; 
        }
        
        .xml-table th { color: #3498db; width: 45%; font-weight: normal; }
        .xml-table td { font-weight: bold; }

        .action-btns { 
            display: flex; 
            flex-direction: column; 
            gap: 1vh; 
            margin-top: 2vh; 
            padding-top: 1.5vh;
            border-top: 2px solid #34495e;
            flex-shrink: 0;
        }
        
        .btn-action { 
            width: 100%; 
            padding: 1.5vh 1vw; 
            border: none; 
            border-radius: 6px; 
            font-weight: bold; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            gap: 8px; 
            transition: all 0.2s; 
            font-size: clamp(0.8rem, 1.1vw, 1.05rem);
        }
        
        .btn-select { background: #f1c40f; color: #000; }
        .btn-select:hover { background: #d4ac0d; }
        .btn-select.selected { background: #2ecc71; color: #fff; }
        .btn-select.selected:hover { background: #27ae60; }
        
        /* JANELA MODAL DO CESTO */
        .modal-cart { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(0,0,0,0.95); 
            z-index: 10000; 
            flex-direction: column; 
        }
        
        .modal-header { 
            padding: 2vh 2vw; 
            background: #2c3e50; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 2px solid #34495e;
            flex-shrink: 0;
        }
        
        .cart-grid { 
            flex-grow: 1; 
            padding: 2vh 2vw; 
            overflow-y: auto; 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); 
            gap: 15px; 
            align-content: start; 
        }
        
        .cart-item { 
            position: relative; 
            border: 2px solid #34495e; 
            border-radius: 8px; 
            background: #000; 
            aspect-ratio: 4 / 3; 
            overflow: hidden;
            display: flex;
            justify-content: center;
            align-items: center;
            cursor: pointer; 
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .cart-item:hover { transform: scale(1.02); box-shadow: 0 0 15px rgba(52, 152, 219, 0.5); }
        .cart-item-img { width: 100%; height: 100%; object-fit: contain; pointer-events: none; }
        
        .remove-mark { 
            position: absolute; 
            top: 5px; 
            right: 5px; 
            background: #e74c3c; 
            color: #fff; 
            width: clamp(25px, 3vw, 30px); 
            height: clamp(25px, 3vw, 30px); 
            border-radius: 50%; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            cursor: pointer; 
            font-size: clamp(10px, 1.2vw, 14px); 
            z-index: 20;
            box-shadow: 0 0 10px rgba(0,0,0,0.5);
            transition: transform 0.2s, background-color 0.2s;
        }
        
        .remove-mark:hover { background: #c0392b; transform: scale(1.1); }
        
        .modal-footer { 
            padding: 2vh 2vw; 
            background: #2c3e50; 
            display: flex; 
            justify-content: center; 
            gap: 2vw; 
            border-top: 2px solid #34495e;
            flex-shrink: 0;
        }
        
        .lbl-cart-info {
            position: absolute;
            bottom: 0; left: 0; right: 0;
            background: rgba(0,0,0,0.7);
            color: #fff;
            font-size: clamp(0.6rem, 1vw, 0.75rem);
            padding: 4px;
            text-align: center;
            pointer-events: none;
        }

        /* SUB-MODAL DE DETALHE E ZOOM FOCADO */
        .modal-detail {
            display: none;
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            background: #000;
            z-index: 10005; 
            flex-direction: column;
        }
        
        .modal-detail-header {
            padding: 1.5vh 2vw;
            background: #1e272e;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #34495e;
            flex-shrink: 0;
        }

        .modal-detail-header p { margin: 0; color: #bdc3c7; font-size: clamp(0.8rem, 1.2vw, 0.9rem); }

        .detail-viewer {
            flex-grow: 1;
            position: relative;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
            cursor: zoom-in;
            min-height: 0;
        }

        .detail-viewer.zoomed { cursor: move; }
        
        .detail-img { 
            max-width: 100%; 
            max-height: 100%; 
            width: auto;
            height: auto;
            transition: transform 0.2s ease-out; 
            pointer-events: none; 
        }
        
        /* OVERLAYS */
        .loading-overlay { 
            position: absolute; 
            top:0; left:0; right:0; bottom:0; 
            background: rgba(0,0,0,0.8); 
            display: flex; 
            flex-direction: column; 
            justify-content: center; 
            align-items: center; 
            z-index: 100000; 
        }
        
        .spinner { 
            border: 8px solid #f3f3f3; 
            border-top: 8px solid #3498db; 
            border-radius: 50%; 
            width: clamp(40px, 6vw, 60px); 
            height: clamp(40px, 6vw, 60px); 
            animation: spin 1s linear infinite; 
            margin-bottom: 2vh; 
        }
        
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        
        /* TIMELINE WIDGET */
        .timeline-container { 
            flex-shrink: 0;
            width: 100%; 
            margin-top: 1.5vh; 
            padding: 0 1vw; 
            box-sizing: border-box; 
            user-select: none; 
            height: clamp(60px, 10vh, 100px); 
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        
        .timeline-labels { 
            display: flex; justify-content: space-between; 
            width: 100%; margin-bottom: 1vh; 
            font-size: clamp(0.7rem, 1vw, 0.9rem); 
            color: #bdc3c7; font-weight: bold; 
        }
        
        .timeline-bar { position: relative; height: clamp(20px, 4vh, 40px); background: transparent; }
        .timeline-bg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: #1e272e; border-radius: 4px; border: 1px solid #2c3e50; overflow: hidden; }
        .timeline-colors { position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block; }
        .color-mark { position: absolute; top: 0; height: 100%; width: 2px; }
        
        .timeline-tick { position: absolute; bottom: 0; background-color: rgba(255,255,255,0.7); z-index: 1; }
        .timeline-tick.hour { width: 2px; height: 15px; background-color: #fff; }
        .timeline-tick.half-hour { width: 1px; height: 8px; }
        
        .timeline-dim-left, .timeline-dim-right { position: absolute; top: 0; height: 100%; background: rgba(0,0,0,0.75); pointer-events: none; z-index: 3; }
        .timeline-dim-left { left: 0; width: 0%; }
        .timeline-dim-right { right: 0; width: 0%; }
        
        .timeline-labels-container { position: relative; height: 2vh; width: 100%; margin-top: 2px; }
        .timeline-tick-label { position: absolute; top: 0; transform: translateX(-50%); font-size: clamp(9px, 1vw, 12px); color: #fff; font-weight: bold; pointer-events: none; }
        
        .timeline-indicator { 
            position: absolute; top: -2px; width: 4px; 
            height: calc(100% + 4px); 
            background: #fff; border-radius: 2px; z-index: 5; 
            transform: translateX(-2px); pointer-events: none; 
            box-shadow: 0 0 5px #fff; display: none; 
        }
        
        .timeline-handle { 
            position: absolute; top: -5px; width: clamp(10px, 1.5vw, 12px); 
            height: calc(100% + 10px); 
            background: #ecf0f1; border: 1px solid #7f8c8d; 
            cursor: ew-resize; border-radius: 4px; z-index: 10; 
            transform: translateX(-50%); box-shadow: 0 0 5px rgba(0,0,0,0.5); 
            display: flex; justify-content: center;
        }
        
        .handle-tooltip {
            position: absolute; top: -25px; background: #f1c40f; color: #000; 
            padding: 2px 6px; border-radius: 4px; font-size: clamp(9px, 1vw, 11px); 
            font-weight: bold; pointer-events: none; white-space: nowrap; display: none;
        }
        .timeline-handle:active .handle-tooltip, .timeline-handle:hover .handle-tooltip { display: block; }
        
        /* Custom Scrollbar */
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #1e272e; }
        ::-webkit-scrollbar-thumb { background: #34495e; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #2c3e50; }
    </style>
</head>
<body>
    <div id="loader" class="loading-overlay">
        <div class="spinner"></div>
        <h2 style="color:#fff; font-size: clamp(1.2rem, 2vw, 2rem); text-align:center;">{{ t('A pesquisar e organizar imagens da NAS...') }}</h2>
        <p style="color:#bdc3c7; font-size: clamp(0.8rem, 1.2vw, 1rem); text-align:center;">{{ t('Operação a utilizar os novos motores de cache ultrarrápidos.') }}</p>
    </div>
    
    <div id="zip_loader" class="loading-overlay" style="display: none;">
        <i class="fas fa-spinner fa-spin fa-3x" style="color:#3498db; margin-bottom: 2vh;"></i>
        <h2 style="color:#fff; margin:0; font-size: clamp(1.2rem, 2vw, 2rem); text-align:center;">{{ t('A preparar Download das Imagens no Servidor...') }}</h2>
    </div>

    <div class="topbar">
        <a href="/" class="btn-back"><i class="fas fa-arrow-left"></i> {{ t('Voltar aos Filtros') }}</a>
        <h2 class="info-title" id="lbl_context">{{ t('Linha') }} -- | {{ t('Máquina') }} --</h2>
        <div id="cart_count_top" style="background: #e67e22; padding: 0.8vh 1vw; border-radius: 20px; font-weight: bold; cursor: pointer; color: white; font-size: clamp(0.8rem, 1.2vw, 1rem); white-space: nowrap;" onclick="openCart()">
            <i class="fas fa-shopping-basket"></i> <span id="cart_count_text">0</span> {{ t('Selecionadas') }}
        </div>
    </div>
    
    <div id="filters_container" class="filters"></div>

    <div class="main-content">
        <div class="viewer-area">
            <div class="image-wrapper">
                <div class="nav-zone nav-left" onclick="prevImg()"><i class="fas fa-chevron-left"></i></div>
                <div class="image-container" id="main_zoom_container">
                    <img id="main_image" class="zoom-img" src="" alt="{{ t('Sem imagem') }}">
                </div>
                <div class="nav-zone nav-right" onclick="nextImg()"><i class="fas fa-chevron-right"></i></div>
            </div>
            
            <div class="timeline-container">
                <div class="timeline-labels">
                    <span id="label-start">--:--</span>
                    <span id="label-current-time" style="color: #3498db; font-size: clamp(1rem, 1.5vw, 16px); font-weight: bold;">--:--:--</span>
                    <span id="label-end">--:--</span>
                </div>
                <div class="timeline-bar" id="timeline-bar">
                    <div class="timeline-bg">
                        <div class="timeline-colors" id="timeline-colors"></div>
                        <div id="timeline-ticks"></div>
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
        
        <div class="data-panel">
            <div class="data-header"><i class="fas fa-file-code"></i> {{ t('Dados da Inspeção (XML)') }}</div>
            <div class="data-content">
                <div style="text-align:center; margin-bottom: 1.5vh; font-weight: bold; color: #2ecc71; font-size: clamp(0.9rem, 1.2vw, 1rem);" id="lbl_progress">{{ t('Imagem') }} 0 {{ t('de') }} 0</div>
                <div class="data-box"><span>{{ t('Data da Imagem') }}</span><strong id="val_data">--/--/----</strong></div>
                <div class="data-box"><span>{{ t('Hora da Imagem') }}</span><strong id="val_hora">--:--:--</strong></div>
                <div class="data-box"><span>{{ t('Turno') }}</span><strong id="val_turno">--</strong></div>
                <div id="xml_container" style="margin-top: 1.5vh;"></div>
                
                <div style="flex-grow: 1;"></div>
                
                <div class="action-btns">
                    <button class="btn-action btn-select" id="btn_toggle_select" onclick="toggleSelection()">
                        <i class="fas fa-plus-circle"></i> {{ t('Selecionar esta Imagem') }}
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <div class="modal-cart" id="modal_cart">
        <div class="modal-header">
            <h2 style="margin:0; color:#fff; font-size: clamp(1.2rem, 2vw, 1.5rem);"><i class="fas fa-images"></i> {{ t('Cesto de Imagens Selecionadas') }}</h2>
            <button class="btn-back" onclick="closeCart()">{{ t('Fechar Janela') }}</button>
        </div>
        <div class="cart-grid" id="cart_grid"></div>
        <div class="modal-footer">
            <button class="btn-action" style="background: #2ecc71; color: #fff; width: auto; padding: 1.5vh 2vw;" onclick="downloadSelectedZIP()">
                <i class="fas fa-download"></i> {{ t('Descarregar Todas (.zip)') }}
            </button>
            <button class="btn-action" style="background: #e74c3c; color: #fff; width: auto; padding: 1.5vh 2vw;" onclick="clearCart()">
                <i class="fas fa-trash-alt"></i> {{ t('Limpar Toda a Seleção') }}
            </button>
        </div>
    </div>

    <div class="modal-detail" id="modal_detail">
        <div class="modal-detail-header">
            <p><i class="fas fa-info-circle"></i> {{ t('Dica: Faça duplo clique na zona exata que deseja ampliar detalhadamente.') }}</p>
            <button class="btn-back" onclick="closeDetailModal()"><i class="fas fa-times"></i> {{ t('Voltar ao Cesto') }}</button>
        </div>
        <div class="detail-viewer" id="detail_zoom_container">
            <img id="detail_image" class="detail-img" src="" alt="">
        </div>
    </div>

    <script>
        const fundoCameras = {
            "21": {nome: "{{ t('Boca 2') }}", cor: "#e74c3c"},
            "25": {nome: "{{ t('Boca 1') }}", cor: "#e67e22"},
            "13": {nome: "{{ t('Stress') }}", cor: "#f1c40f"},
            "28": {nome: "{{ t('Fundo 2') }}", cor: "#2ecc71"},
            "11": {nome: "{{ t('Fundo') }}", cor: "#27ae60"},
            "15": {nome: "{{ t('Leitor 2') }}", cor: "#3498db"},
            "24": {nome: "{{ t('Leitor') }}", cor: "#2980b9"},
            "22": {nome: "{{ t('Wire Edge') }}", cor: "#9b59b6"},
            "indefinido": {nome: "{{ t('Indef.') }}", cor: "#95a5a6"}
        };
        const lateralNormalTop = {
            "13": {nome: "{{ t('Câmara') }} 13", cor: "#3498db"},
            "33": {nome: "{{ t('Câmara') }} 33", cor: "#00bcd4"},
            "14": {nome: "{{ t('Câmara') }} 14", cor: "#2ecc71"},
            "23": {nome: "{{ t('Câmara') }} 23", cor: "#8bc34a"},
            "34": {nome: "{{ t('Câmara') }} 34", cor: "#f1c40f"},
            "24": {nome: "{{ t('Câmara') }} 24", cor: "#ff9800"}
        };
        const lateralNormalBottom = {
            "11": {nome: "{{ t('Câmara') }} 11", cor: "#e91e63"},
            "31": {nome: "{{ t('Câmara') }} 31", cor: "#9c27b0"},
            "12": {nome: "{{ t('Câmara') }} 12", cor: "#3f51b5"},
            "21": {nome: "{{ t('Câmara') }} 21", cor: "#009688"},
            "32": {nome: "{{ t('Câmara') }} 32", cor: "#795548"},
            "22": {nome: "{{ t('Câmara') }} 22", cor: "#607d8b"}
        };
        const lateralStress = {
            "41": {nome: "{{ t('Stress') }} 41", cor: "#c0392b"},
            "42": {nome: "{{ t('Stress') }} 42", cor: "#e74c3c"},
            "43": {nome: "{{ t('Stress') }} 43", cor: "#d2b4de"},
            "44": {nome: "{{ t('Stress') }} 44", cor: "#f5b041"}
        };
        const lateralCamerasAll = {...lateralNormalTop, ...lateralNormalBottom, ...lateralStress, "indefinido": {nome: "{{ t('Indef.') }}", cor: "#95a5a6"}};

        let allFetchedImages = [];
        let imagesList = [];
        let currentIndex = 0;
        let payload = null;
        let imageCache = {};
        
        let selectedImages = new Map();
        
        let timelineStartSec = 0;
        let timelineEndSec = 0;
        let totalDuration = 0;
        let minPct = 0;
        let maxPct = 100;
        
        document.addEventListener('DOMContentLoaded', () => {
            const dataStr = sessionStorage.getItem('ext_history_payload');
            if(!dataStr) { alert("{{ t('Nenhuma seleção. Redirecionando...') }}"); window.location.href = '/'; return; }
            payload = JSON.parse(dataStr);
            document.getElementById('lbl_context').innerText = `{{ t('Linha') }} ${payload.linha} | {{ t('Máquina') }} ${payload.maquina.toUpperCase()}`;
            
            buildFilters(payload.maquina);
            initTimelineDrag();
            
            initStandardZoom(document.getElementById('main_zoom_container'), document.getElementById('main_image'));
            initFocusedZoom(document.getElementById('detail_zoom_container'), document.getElementById('detail_image'));
            
            fetchImages();
        });
        
        function initStandardZoom(container, imgElement) {
            let isZoomed = false;
            const ZOOM_SCALE = 3; 
            container.addEventListener('dblclick', function(e) {
                isZoomed = !isZoomed;
                container.classList.toggle('zoomed', isZoomed);
                if (isZoomed) {
                    imgElement.style.transform = `scale(${ZOOM_SCALE})`;
                    imgElement.style.cursor = 'zoom-out';
                    updateTransformOrigin(e);
                } else {
                    imgElement.style.transform = 'scale(1)';
                    imgElement.style.transformOrigin = 'center center';
                    imgElement.style.cursor = 'zoom-in';
                }
            });
            container.addEventListener('mousemove', function(e) {
                if (!isZoomed) return;
                updateTransformOrigin(e);
            });
            container.addEventListener('mouseleave', function() {
                if (isZoomed) {
                    isZoomed = false;
                    container.classList.remove('zoomed');
                    imgElement.style.transform = 'scale(1)';
                    imgElement.style.transformOrigin = 'center center';
                    imgElement.style.cursor = 'zoom-in';
                }
            });
            function updateTransformOrigin(e) {
                const rect = container.getBoundingClientRect();
                const x = ((e.clientX - rect.left) / rect.width) * 100;
                const y = ((e.clientY - rect.top) / rect.height) * 100;
                imgElement.style.transformOrigin = `${x}% ${y}%`;
            }
        }

        function initFocusedZoom(container, imgElement) {
            let isZoomed = false;
            const ZOOM_SCALE = 4; 
            container.addEventListener('dblclick', function(e) {
                isZoomed = !isZoomed;
                container.classList.toggle('zoomed', isZoomed);
                if (isZoomed) {
                    updateTransformOrigin(e);
                    imgElement.style.transform = `scale(${ZOOM_SCALE})`;
                    imgElement.style.cursor = 'zoom-out';
                } else {
                    imgElement.style.transform = 'scale(1)';
                    imgElement.style.transformOrigin = 'center center';
                    imgElement.style.cursor = 'zoom-in';
                }
            });
            container.addEventListener('mousemove', function(e) {
                if (!isZoomed) return;
                updateTransformOrigin(e);
            });
            container.addEventListener('mouseleave', function() {
                if (isZoomed) {
                    isZoomed = false;
                    container.classList.remove('zoomed');
                    imgElement.style.transform = 'scale(1)';
                    imgElement.style.transformOrigin = 'center center';
                    imgElement.style.cursor = 'zoom-in';
                }
            });
            function updateTransformOrigin(e) {
                const rect = container.getBoundingClientRect();
                const x = ((e.clientX - rect.left) / rect.width) * 100;
                const y = ((e.clientY - rect.top) / rect.height) * 100;
                imgElement.style.transformOrigin = `${x}% ${y}%`;
            }
        }
        
        function formatSecToTime(sec) {
            let d = new Date(sec * 1000);
            return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
        }
        
        function getOffsetPct(imgEpoch) {
            if (totalDuration === 0) return 0;
            let pct = ((imgEpoch - timelineStartSec) / totalDuration) * 100;
            return Math.max(0, Math.min(100, pct));
        }
        
        function getColor(cam_id) {
            if (payload.maquina.toLowerCase().includes('fundo') && fundoCameras[cam_id]) return fundoCameras[cam_id].cor;
            if (payload.maquina.toLowerCase().includes('lateral') && lateralCamerasAll[cam_id]) return lateralCamerasAll[cam_id].cor;
            return '#95a5a6';
        }

        function drawTimelineTicks() {
            const ticksContainer = document.getElementById('timeline-ticks');
            const labelsContainer = document.getElementById('timeline-labels-container');
            ticksContainer.innerHTML = '';
            if(labelsContainer) labelsContainer.innerHTML = '';
            
            if (totalDuration <= 0) return;
            
            let step = 1800; 
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
        
        function buildFilters(maquina) {
            const container = document.getElementById('filters_container');
            const isFundo = maquina.toLowerCase().includes('fundo');
            const isLateral = maquina.toLowerCase().includes('lateral');

            let html = '';
            if (isFundo) {
                html += `<div style="background-color: #2c3e50; padding: 1vh 1vw; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
                    <div class="filters-row">`;
                for (const [cam_id, cam_info] of Object.entries(fundoCameras)) {
                    html += `<label class="filter-btn" style="border-bottom: 4px solid ${cam_info.cor};">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="${cam_id}" checked onchange="applyFilters()"> 
                            <strong>${cam_info.nome}</strong>
                        </div>
                        <span id="count-${cam_id}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                    </label>`;
                }
                html += `</div></div>`;
            } else if (isLateral) {
                html += `<div style="display: flex; width: 100%; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 8px rgba(0,0,0,0.3);">
                    <div style="flex: 2; background-color: #ecf0f1; padding: 0.5vh 1vw; color: #2c3e50; display: flex; flex-direction: column; justify-content: center;">
                        <div class="filters-row" style="margin-bottom: 4px;">`;
                for (const [cam_id, cam_info] of Object.entries(lateralNormalTop)) {
                    html += `<label class="lateral-normal-cb filter-btn" style="border-bottom: 4px solid ${cam_info.cor};">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="${cam_id}" checked onchange="applyFilters()"> 
                            <strong>${cam_info.nome}</strong>
                        </div>
                        <span id="count-${cam_id}" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                    </label>`;
                }
                html += `<label class="lateral-normal-cb filter-btn" style="visibility: hidden; pointer-events: none;"></label>`;
                html += `</div><div class="filters-row">`;
                for (const [cam_id, cam_info] of Object.entries(lateralNormalBottom)) {
                    html += `<label class="lateral-normal-cb filter-btn" style="border-bottom: 4px solid ${cam_info.cor};">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="${cam_id}" checked onchange="applyFilters()"> 
                            <strong>${cam_info.nome}</strong>
                        </div>
                        <span id="count-${cam_id}" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                    </label>`;
                }
                html += `<label class="lateral-normal-cb filter-btn" style="border-bottom: 4px solid #95a5a6;">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="indefinido" checked onchange="applyFilters()"> 
                            <strong>{{ t('Indef.') }}</strong>
                        </div>
                        <span id="count-indefinido" class="img-count" style="color: #e67e22;">(0 imgs)</span>
                    </label>
                </div></div>
                <div style="flex: 1; background-color: #111; padding: 0.5vh 1vw; color: #fff; border-left: 2px solid #34495e; display: flex; flex-direction: column; justify-content: center;">
                        <div class="filters-row" style="margin-bottom: 4px;">`;
                
                const stressKeys = Object.keys(lateralStress);
                for(let i=0; i<2; i++) {
                    const cam_id = stressKeys[i];
                    const cam_info = lateralStress[cam_id];
                    html += `<label class="lateral-stress-cb filter-btn" style="border-bottom: 4px solid ${cam_info.cor};">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="${cam_id}" checked onchange="applyFilters()"> 
                            <strong>${cam_info.nome}</strong>
                        </div>
                        <span id="count-${cam_id}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                    </label>`;
                }
                html += `</div><div class="filters-row">`;
                for(let i=2; i<4; i++) {
                    const cam_id = stressKeys[i];
                    const cam_info = lateralStress[cam_id];
                    html += `<label class="lateral-stress-cb filter-btn" style="border-bottom: 4px solid ${cam_info.cor};">
                        <div class="filter-label-wrapper">
                            <input type="checkbox" name="camera" value="${cam_id}" checked onchange="applyFilters()"> 
                            <strong>${cam_info.nome}</strong>
                        </div>
                        <span id="count-${cam_id}" class="img-count" style="color: #f1c40f;">(0 imgs)</span>
                    </label>`;
                }
                html += `</div></div></div>`;
            } else {
                html += `<p style="color: #e74c3c;">{{ t('Filtros não suportados para esta máquina.') }}</p>`;
            }
            container.innerHTML = html;
        }

        function fetchImages() {
            fetch('/api/ext_history/images', {
                method: 'POST', 
                headers: {'Content-Type': 'application/json'}, 
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(data => {
                allFetchedImages = data.images || [];
                document.getElementById('loader').style.display = 'none';
                
                if (data.counts) {
                    for (const [camId, count] of Object.entries(data.counts)) {
                        const countEl = document.getElementById('count-' + camId);
                        if (countEl) countEl.innerText = `(${count} imgs)`;
                    }
                }

                if(allFetchedImages.length === 0) { 
                    alert("{{ t('Sem imagens para a seleção efetuada.') }}"); 
                    window.location.href = '/'; 
                    return; 
                }
                
                allFetchedImages.forEach(img => {
                    img.linha = payload.linha;
                    img.maquina = payload.maquina;
                });
                
                if (allFetchedImages.length > 0) {
                    let mtimes = allFetchedImages.map(i => i.mtime);
                    timelineStartSec = Math.min(...mtimes);
                    timelineEndSec = Math.max(...mtimes);
                    if (timelineStartSec === timelineEndSec) {
                        timelineStartSec -= 1800;
                        timelineEndSec += 1800;
                    }
                    totalDuration = timelineEndSec - timelineStartSec;
                    
                    let startD = new Date(timelineStartSec * 1000);
                    let endD = new Date(timelineEndSec * 1000);
                    document.getElementById('label-start').innerText = startD.toLocaleDateString() + ' ' + formatSecToTime(timelineStartSec);
                    document.getElementById('label-end').innerText = endD.toLocaleDateString() + ' ' + formatSecToTime(timelineEndSec);
                    
                    drawTimelineTicks();
                }
                
                applyFilters();
            }).catch(err => { 
                alert("{{ t('Erro de comunicação.') }}"); 
                window.location.href = '/'; 
            });
        }
        
        function applyFilters() {
            const checkboxes = document.querySelectorAll('input[name="camera"]');
            let validHistoryImages = allFetchedImages;
            
            if (checkboxes.length > 0) {
                const selectedCams = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
                validHistoryImages = allFetchedImages.filter(img => selectedCams.includes(img.cam_id));
            } 
            
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
            
            imagesList = validHistoryImages.filter(img => {
                let p = getOffsetPct(img.mtime);
                return p >= minPct && p <= maxPct;
            });
            
            if(imagesList.length === 0) {
                document.getElementById('lbl_progress').innerText = `{{ t('Imagem') }} 0 {{ t('de') }} 0`;
                document.getElementById('main_image').src = '';
                document.getElementById('main_image').style.border = '5px solid transparent';
                document.getElementById('val_data').innerText = '--/--/----';
                document.getElementById('val_turno').innerText = '--';
                document.getElementById('val_hora').innerText = "--:--:--";
                document.getElementById('xml_container').innerHTML = `<p style="text-align:center; color:#e74c3c;">{{ t('Nenhuma imagem corresponde aos filtros.') }}</p>`;
                return;
            }

            currentIndex = 0;
            renderCurrentImage();
        }

        function manageImageCache(centerIndex) {
            const radius = 25; 
            const keepSet = new Set();
            for(let i = Math.max(0, centerIndex - radius); i <= Math.min(imagesList.length - 1, centerIndex + radius); i++) {
                keepSet.add(i);
                if(!imageCache[i]) {
                    imageCache[i] = new Image();
                    imageCache[i].src = imagesList[i].url;
                }
            }
            for(let k in imageCache) {
                if(!keepSet.has(parseInt(k))) {
                    delete imageCache[k];
                }
            }
        }

        function renderCurrentImage() {
            if(imagesList.length === 0) return;
            
            manageImageCache(currentIndex);
            
            const currentData = imagesList[currentIndex];
            const maquinaStr = payload.maquina.toLowerCase();
            
            document.getElementById('lbl_progress').innerText = `{{ t('Imagem') }} ${currentIndex + 1} {{ t('de') }} ${imagesList.length}`;
            
            const imgEl = document.getElementById('main_image');
            const container = document.getElementById('main_zoom_container');
            container.classList.remove('zoomed');
            imgEl.style.transform = 'scale(1)';
            imgEl.style.transformOrigin = 'center center';
            imgEl.style.cursor = 'zoom-in';
            
            imgEl.src = currentData.url;
            
            let cId = currentData.cam_id ? currentData.cam_id.toString().trim() : 'indefinido';
            let borderCol = 'transparent';
            if (maquinaStr.includes('fundo') && fundoCameras[cId]) {
                borderCol = fundoCameras[cId].cor;
            } else if (maquinaStr.includes('lateral') && lateralCamerasAll[cId]) {
                borderCol = lateralCamerasAll[cId].cor;
            }
            imgEl.style.border = `5px solid ${borderCol}`;
            
            document.getElementById('val_data').innerText = currentData.date;
            document.getElementById('val_turno').innerText = currentData.shift.toUpperCase();
            document.getElementById('val_hora').innerText = "{{ t('A carregar...') }}";
            document.getElementById('xml_container').innerHTML = `<p style="text-align:center; color:#bdc3c7;"><i class="fas fa-spinner fa-spin"></i> {{ t('A ler XML...') }}</p>`;
            
            const btn = document.getElementById('btn_toggle_select');
            if(selectedImages.has(currentData.url)) {
                btn.classList.add('selected');
                btn.innerHTML = '<i class="fas fa-check-circle"></i> {{ t("Remover da Seleção") }}';
            } else {
                btn.classList.remove('selected');
                btn.innerHTML = '<i class="fas fa-plus-circle"></i> {{ t("Selecionar esta Imagem") }}';
            }
            
            const indicator = document.getElementById('current-indicator');
            if (indicator) {
                indicator.style.display = 'block';
                indicator.style.left = getOffsetPct(currentData.mtime) + '%';
                document.getElementById('label-current-time').innerText = formatSecToTime(currentData.mtime);
            }
            
            fetch(`/api/ext_history/xml_data?linha=${payload.linha}&maquina=${payload.maquina}&date=${currentData.date}&shift=${currentData.shift}&file=${currentData.filename}`)
            .then(r => r.json()).then(xmlData => {
                if(xmlData.error) {
                    document.getElementById('val_hora').innerText = "--:--:--";
                    document.getElementById('xml_container').innerHTML = `<p style="color:#e74c3c; text-align:center;">${xmlData.error}</p>`;
                    return;
                }
                document.getElementById('val_hora').innerText = xmlData._time || "--:--:--";
                delete xmlData._time;
                let tableHtml = '<table class="xml-table"><tbody>';
                for (const [key, value] of Object.entries(xmlData)) { 
                    tableHtml += `<tr><th>${key}</th><td>${value}</td></tr>`; 
                }
                tableHtml += '</tbody></table>';
                document.getElementById('xml_container').innerHTML = tableHtml;
            }).catch(() => { 
                document.getElementById('xml_container').innerHTML = `<p style="color:#e74c3c;">{{ t('Erro de comunicação.') }}</p>`; 
            });
        }
        
        function toggleSelection() {
            const data = imagesList[currentIndex];
            if(selectedImages.has(data.url)) {
                selectedImages.delete(data.url);
            } else {
                selectedImages.set(data.url, data);
            }
            updateCartUI();
            renderCurrentImage();
        }

        function updateCartUI() {
            const count = selectedImages.size;
            document.getElementById('cart_count_text').innerText = count;
        }

        function openCart() {
            const grid = document.getElementById('cart_grid');
            grid.innerHTML = '';
            
            selectedImages.forEach((imgData, url) => {
                const div = document.createElement('div');
                div.className = 'cart-item';
                div.id = `cart_item_${imgData.mtime}`;
                div.onclick = function() { openDetailModal(url); };
                
                div.innerHTML = `
                    <img src="${url}" class="cart-item-img">
                    <div class="remove-mark" onclick="removeImage('${url}', event)" title="{{ t('Remover Imagem Desta Lista') }}">
                        <i class="fas fa-times"></i>
                    </div>
                    <div class="lbl-cart-info">${imgData.date} | ${imgData.shift}</div>
                `;
                grid.appendChild(div);
            });
            
            document.getElementById('modal_cart').style.display = 'flex';
        }

        function openDetailModal(url) {
            const modal = document.getElementById('modal_detail');
            const img = document.getElementById('detail_image');
            const container = document.getElementById('detail_zoom_container');
            
            img.src = url;
            container.classList.remove('zoomed');
            img.style.transform = 'scale(1)';
            img.style.transformOrigin = 'center center';
            img.style.cursor = 'zoom-in';
            
            modal.style.display = 'flex';
        }

        function closeDetailModal() {
            document.getElementById('modal_detail').style.display = 'none';
            document.getElementById('detail_image').src = '';
        }

        function removeImage(url, event) {
            event.stopPropagation(); 
            selectedImages.delete(url);
            updateCartUI();
            openCart(); 
            renderCurrentImage(); 
        }

        function clearCart() {
            if(selectedImages.size === 0) return;
            if(confirm("{{ t('Deseja realmente limpar a seleção atual e remover todas as imagens assinaladas do cesto?') }}")) {
                selectedImages.clear();
                updateCartUI();
                closeCart();
                renderCurrentImage();
            }
        }

        function closeCart() { 
            document.getElementById('modal_cart').style.display = 'none'; 
        }

        function downloadSelectedZIP() {
            if(selectedImages.size === 0) {
                alert("{{ t('Não existem imagens selecionadas. Marque as imagens antes de proceder ao download.') }}");
                return;
            }
            
            document.getElementById('zip_loader').style.display = 'flex';
            const imagesArray = Array.from(selectedImages.values());
            
            fetch('/api/ext_history/download_zip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ images: imagesArray })
            })
            .then(resp => {
                if(!resp.ok) throw new Error("Falha no ZIP");
                return resp.blob();
            })
            .then(blob => {
                document.getElementById('zip_loader').style.display = 'none';
                
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                a.download = `Selecao_Inspecoes_${new Date().getTime()}.zip`;
                document.body.appendChild(a);
                a.click();
                
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
            })
            .catch(err => {
                document.getElementById('zip_loader').style.display = 'none';
                alert("{{ t('Ocorreu um erro interno de I/O ao processar ou contactar o servidor do ficheiro ZIP.') }}");
            });
        }
        
        function prevImg() { 
            if(currentIndex < imagesList.length - 1) { 
                currentIndex++; 
                renderCurrentImage(); 
            } 
        }
        function nextImg() { 
            if(currentIndex > 0) { 
                currentIndex--; 
                renderCurrentImage(); 
            } 
        }
        
        document.addEventListener('keydown', function(event) {
            if (event.key === 'ArrowLeft') prevImg();
            else if (event.key === 'ArrowRight') nextImg();
        });
    </script>
</body>
</html>
"""

# ==============================================================================
# ROTAS DO PORTAL HISTÓRICO EXTERNO
# ==============================================================================
@app.route('/')
def ext_history_index():
    config = load_config()
    linhas_disponiveis = sorted(list(config.get('linhas', {}).keys()))
    return render_template_string(EXT_HIST_FILTER_TEMPLATE, linhas=linhas_disponiveis)

@app.route('/view')
def ext_history_viewer():
    return render_template_string(EXT_HIST_VIEWER_TEMPLATE)

@app.route('/api/ext_history/machines/<linha>')
def api_ext_history_machines(linha):
    config = load_config()
    maquinas = config.get('linhas', {}).get(linha, {})
    return jsonify({"machines": [k for k, v in maquinas.items() if isinstance(v, dict)]})

@app.route('/api/ext_history/structure/<linha>/<maquina>')
def api_ext_history_structure(linha, maquina):
    config = load_config()
    dst_path = get_active_dst_path(config, linha, maquina)
    
    if not dst_path or not os.path.exists(dst_path): 
        return jsonify({})
        
    struct = {}
    
    try:
        with os.scandir(dst_path) as entries:
            for d_name_entry in entries:
                if not d_name_entry.is_dir() or not re.match(r"^\d{4}-\d{2}-\d{2}$", d_name_entry.name):
                    continue
                y, m, d = d_name_entry.name.split('-')
                if y not in struct: struct[y] = {}
                if m not in struct[y]: struct[y][m] = {}
                if d not in struct[y][m]: struct[y][m][d] = []
                
                with os.scandir(d_name_entry.path) as shift_entries:
                    for shift_dir_entry in shift_entries:
                        if shift_dir_entry.is_dir():
                            struct[y][m][d].append(shift_dir_entry.name)
    except OSError as e:
        logging.error(f"Erro de I/O em {dst_path}: {e}")

    return jsonify(struct)

@app.route('/api/ext_history/artigos')
def api_ext_history_artigos():
    if os.path.exists(ARTIGOS_DB_FILE):
        try:
            with open(ARTIGOS_DB_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception as e:
            logging.error(f"Erro a ler JSON dos artigos: {e}")
            return jsonify({})
    return jsonify({})

@app.route('/api/ext_history/images', methods=['POST'])
def api_ext_history_images():
    data = request.json
    linha = data.get('linha')
    maquina = data.get('maquina')
    selections = data.get('selections', [])
    config = load_config()
    
    dst_path = get_active_dst_path(config, linha, maquina)
    
    if not dst_path: 
        return jsonify({"images": [], "counts": {}})
    
    all_images = []
    counts = {}
    
    for sel in selections:
        date_folder = sel.get('date')
        shifts = sel.get('shifts', [])
        for shift in shifts:
            shift_path = os.path.join(dst_path, date_folder, shift)
            
            jpgs_in_shift = get_cached_jpgs(shift_path)
            for img_obj in jpgs_in_shift:
                jpg_path = img_obj["path"]
                
                xml_path_lower = os.path.splitext(jpg_path)[0] + '.xml'
                xml_path_upper = os.path.splitext(jpg_path)[0] + '.XML'
                xml_path = xml_path_upper if os.path.exists(xml_path_upper) else xml_path_lower
                
                cam_id = "indefinido"

                if xml_path in _hist_xml_cache:
                    cam_id = _hist_xml_cache[xml_path]
                elif os.path.exists(xml_path):
                    try:
                        tree = ET.parse(xml_path)
                        root = tree.getroot()
                        cam_elem = root.find("NUM_CAM")
                        if cam_elem is not None and cam_elem.text:
                            cam_id = str(cam_elem.text).strip()
                        if len(_hist_xml_cache) > 100000:
                            _hist_xml_cache.clear()
                        _hist_xml_cache[xml_path] = cam_id
                    except Exception:
                        pass
                        
                counts[cam_id] = counts.get(cam_id, 0) + 1
                
                all_images.append({
                    "url": f"/api/ext_history/image?l={linha}&m={maquina}&d={date_folder}&s={shift}&f={img_obj['name']}",
                    "date": date_folder, 
                    "shift": shift, 
                    "filename": img_obj['name'], 
                    "mtime": img_obj['mtime'],
                    "cam_id": cam_id
                })
                
    all_images.sort(key=lambda x: x['mtime'], reverse=True)
    return jsonify({"images": all_images, "counts": counts})

@app.route('/api/ext_history/xml_data')
def api_ext_history_xml():
    l = request.args.get('linha')
    m = request.args.get('maquina')
    d = request.args.get('date')
    s = request.args.get('shift')
    f = request.args.get('file')
    config = load_config()
    
    dst_path = get_active_dst_path(config, l, m)
    
    if not dst_path: 
        return jsonify({"error": _t("Configuração da máquina não encontrada ou pasta de destino não configurada.")})
        
    jpg_path = os.path.join(dst_path, d, s, f)
    xml_path_lower = os.path.splitext(jpg_path)[0] + '.xml'
    xml_path_upper = os.path.splitext(jpg_path)[0] + '.XML'
    xml_path = xml_path_upper if os.path.exists(xml_path_upper) else xml_path_lower
    
    if not os.path.exists(xml_path): 
        return jsonify({"error": _t("Ficheiro XML Ausente.")})
        
    try:
        mtime = os.path.getmtime(jpg_path)
        dt = datetime.fromtimestamp(mtime).strftime('%H:%M:%S')
        
        xml_dict = {"_time": dt}
        
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for child in root:
            tag_translated = _t(str(child.tag))
            val_translated = _t(str(child.text).strip()) if child.text else ""
            xml_dict[tag_translated] = val_translated
            
        return jsonify(xml_dict)
    except Exception as e: 
        return jsonify({"error": f"{_t('Erro a ler XML:')} {str(e)}"})

@app.route('/api/ext_history/image')
def api_ext_history_serve_image():
    l = request.args.get('l')
    m = request.args.get('m')
    d = request.args.get('d')
    s = request.args.get('s')
    f = request.args.get('f')
    config = load_config()
    
    dst_path = get_active_dst_path(config, l, m)
    
    if not dst_path: return abort(404)
    return send_from_directory(os.path.join(dst_path, d, s), f, max_age=2592000)

@app.route('/api/ext_history/download_zip', methods=['POST'])
def api_ext_history_download_zip():
    data = request.json
    images = data.get('images', [])
    config = load_config()
    
    memory_file = io.BytesIO()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            l = img.get('linha')
            m = img.get('maquina')
            d = img.get('date')
            s = img.get('shift')
            f = img.get('filename')
            
            dst_path = get_active_dst_path(config, l, m)
            if dst_path:
                file_path = os.path.join(dst_path, d, s, f)
                if os.path.exists(file_path):
                    zf.write(file_path, arcname=f"{d}_{s}_{f}")
    
    memory_file.seek(0)
    
    return send_file(
        memory_file, 
        download_name=f"Imagens_Selecionadas_{int(time.time())}.zip", 
        as_attachment=True, 
        mimetype='application/zip'
    )

if __name__ == '__main__':
    port = 5581
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    logging.info(f"Iniciando Portal de Histórico Público OTIMIZADO na porta {port}...")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True, use_reloader=False)