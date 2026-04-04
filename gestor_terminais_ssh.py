#!/usr/bin/env python3
"""
Portal Autónomo: Gestor de Terminais Remotos via SSH
Permite adicionar terminais por IP, gerir credenciais, executar comandos remotos
e configurar o sistema operativo (Hostname, Rotação, Screensaver, IP Fixo/DHCP, Wi-Fi, NTP, URL)
diretamente a partir de uma interface Web unificada.
Inclui Scanner de Rede Multi-Thread, Lista Automática de URLs de Mosaico (Lida do JSON), 
leitura em tempo real do status corrente, teste de acessibilidade do URL (LED),
Diagnóstico Avançado de Hardware e Proteção Read-Only do Cartão SD.
Corre num processo isolado (Porta 5583) para estabilidade da NAS.
"""

import os
import json
import logging
import base64
import threading
import socket
import time
import subprocess
import platform
import concurrent.futures
from flask import Flask, render_template_string, request, jsonify

# Tentar importar a biblioteca SSH paramiko (Obrigatória para gestão segura)
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    print("AVISO: A biblioteca 'paramiko' não está instalada. Execute: pip install paramiko")

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TERMINALS_DB = os.path.join(DATA_DIR, "terminais_ssh.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ==============================================================================
# INICIALIZAÇÃO
# ==============================================================================
app = Flask(__name__)
app.secret_key = os.urandom(24)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [GESTOR SSH] - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

db_lock = threading.Lock()

# ==============================================================================
# LÓGICA DE DADOS (ENCRIPTAÇÃO BÁSICA E GESTÃO DE JSON)
# ==============================================================================
def encode_pwd(clear_text):
    return base64.b64encode(clear_text.encode('utf-8')).decode('utf-8')

def decode_pwd(encoded_text):
    try:
        return base64.b64decode(encoded_text.encode('utf-8')).decode('utf-8')
    except:
        return ""

def load_terminals():
    with db_lock:
        if os.path.exists(TERMINALS_DB):
            try:
                with open(TERMINALS_DB, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Erro ao carregar DB de terminais: {e}")
        return {}

def save_terminals(data):
    with db_lock:
        try:
            with open(TERMINALS_DB, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            return True
        except Exception as e:
            logging.error(f"Erro ao guardar DB de terminais: {e}")
            return False

def is_reachable(ip_addr):
    """ Verifica se um IP responde a PING. Compatível com Linux e Windows. """
    try:
        param = '-c' if platform.system().lower() != 'windows' else '-n'
        timeout_param = '-W' if platform.system().lower() != 'windows' else '-w'
        timeout_val = '2' if platform.system().lower() != 'windows' else '2000'
        cmd = ['ping', param, '1', timeout_param, timeout_val, ip_addr]
        return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except Exception:
        return False

# Analisador Recursivo: Procura portas perdidas em qualquer estrutura de JSON
def find_ports_in_json(data, prefix=""):
    ports = []
    if isinstance(data, dict):
        for k, v in data.items():
            if str(k).lower() in ['ip', 'id', 'password', 'estado']: continue
            new_prefix = f"{prefix} {k}".strip() if prefix else k
            if isinstance(v, int) and 1000 < v < 9999:
                ports.append({"name": new_prefix, "port": v})
            elif isinstance(v, str) and v.isdigit() and 1000 < int(v) < 9999:
                ports.append({"name": new_prefix, "port": int(v)})
            elif isinstance(v, (dict, list)):
                ports.extend(find_ports_in_json(v, new_prefix))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            if isinstance(item, (dict, list)):
                item_name = item.get('nome', item.get('name', f"Linha {idx+1}")) if isinstance(item, dict) else f"Item {idx+1}"
                new_prefix = f"{prefix} {item_name}".strip() if prefix else item_name
                ports.extend(find_ports_in_json(item, new_prefix))
    return ports

# ==============================================================================
# INTERFACE GRÁFICA (HTML / CSS / JS)
# ==============================================================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <title>Gestor de Terminais SSH</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e2f; color: #e0e0e0; min-height: 100vh; overflow-x: hidden; }
        
        .header { background: #282a36; padding: 1.5rem; box-shadow: 0 4px 15px rgba(0,0,0,0.5); display: flex; justify-content: space-between; align-items: center; border-bottom: 3px solid #ff79c6; }
        .header h1 { color: #f8f8f2; font-size: 1.8rem; display: flex; align-items: center; gap: 10px; }
        .header h1 i { color: #ff79c6; }
        
        .container { max-width: 1500px; margin: 2rem auto; padding: 0 2rem; display: grid; grid-template-columns: 380px 1fr; gap: 2rem; }
        @media (max-width: 1024px) { .container { grid-template-columns: 1fr; } }
        
        .panel { background: #282a36; border-radius: 10px; padding: 1.5rem; box-shadow: 0 8px 20px rgba(0,0,0,0.3); border: 1px solid #44475a; }
        .panel h3 { color: #8be9fd; margin-bottom: 1.5rem; font-size: 1.2rem; border-bottom: 1px solid #44475a; padding-bottom: 0.5rem; }
        
        .form-group { margin-bottom: 1.2rem; }
        .form-group label { display: block; margin-bottom: 0.5rem; color: #f8f8f2; font-weight: 500; font-size: 0.9rem; }
        .form-control { width: 100%; padding: 0.8rem; background: #1e1e2f; border: 1px solid #6272a4; border-radius: 6px; color: #f8f8f2; font-size: 1rem; transition: border-color 0.3s; }
        .form-control:focus { outline: none; border-color: #bd93f9; box-shadow: 0 0 0 2px rgba(189, 147, 249, 0.3); }
        
        /* NOVO DESIGN DE BOTOES (3D + UNIFORMIDADE) */
        .btn { 
            position: relative;
            overflow: hidden;
            padding: 0.8rem 1rem; 
            border: none; 
            border-radius: 6px; 
            cursor: pointer; 
            font-weight: bold; 
            font-size: 0.9rem; 
            display: inline-flex; 
            align-items: center; 
            justify-content: center;
            gap: 0.5rem; 
            width: 100%; 
            transition: background-color 0.2s, transform 0.1s ease, box-shadow 0.1s ease; 
            box-shadow: 0 5px 0px rgba(0,0,0,0.3), 0 5px 10px rgba(0,0,0,0.2);
            transform: translateY(0);
        }
        
        .btn:active { transform: translateY(5px); box-shadow: 0 0px 0px rgba(0,0,0,0.3), 0 0px 0px rgba(0,0,0,0.2); }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: translateY(5px); box-shadow: none; }
        
        @keyframes shimmer-anim { 0% { transform: translateX(-150%); } 100% { transform: translateX(150%); } }
        .shimmer-effect::after {
            content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background: linear-gradient(90deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.6) 50%, rgba(255,255,255,0) 100%);
            animation: shimmer-anim 0.5s ease-out; pointer-events: none;
        }
        
        .btn-group { display: flex; gap: 10px; width: 100%; margin-bottom: 15px; }
        .btn-group .btn { flex: 1; padding: 0.8rem 0.2rem; font-size: 0.85rem; white-space: nowrap; }
        .input-btn-group { display: flex; gap: 10px; width: 100%; margin-bottom: 15px; align-items: stretch; }
        .input-btn-group .form-control { flex: 2; margin: 0; }
        .input-btn-group .btn { flex: 1; margin: 0; width: auto; padding: 0 0.5rem; }

        .btn-success { background: #50fa7b; color: #282a36; }
        .btn-danger { background: #ff5555; color: #f8f8f2; }
        .btn-warning { background: #ffb86c; color: #282a36; }
        .btn-primary { background: #bd93f9; color: #f8f8f2; }
        .btn-info { background: #8be9fd; color: #282a36; }
        .btn-secondary { background: #6272a4; color: #f8f8f2; }
        
        .terminal-list { display: flex; flex-direction: column; gap: 1rem; max-height: 600px; overflow-y: auto; padding-right: 5px; }
        .terminal-card { background: #383a59; border-radius: 8px; padding: 1rem; border-left: 4px solid #6272a4; transition: all 0.2s; display: flex; justify-content: space-between; align-items: center; }
        .terminal-card:hover { background: #44475a; border-left-color: #bd93f9; }
        .terminal-info h4 { color: #f8f8f2; margin-bottom: 0.3rem; font-size: 1.1rem; }
        .terminal-info p { color: #8be9fd; font-family: monospace; font-size: 0.9rem; margin-bottom: 0.3rem; }
        .terminal-info small { color: #6272a4; font-size: 0.8rem; }
        
        .terminal-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; justify-content: flex-end; flex: 1; min-width: 250px; }
        .terminal-actions .btn { width: auto; font-size: 0.85rem; padding: 0.5rem 1rem; box-shadow: 0 3px 0px rgba(0,0,0,0.3); }
        .terminal-actions .btn:active { transform: translateY(3px); box-shadow: none; }
        
        .console-window { background: #000; border-radius: 8px; border: 1px solid #44475a; height: 350px; width: 100%; margin-top: 1.5rem; padding: 1rem; overflow-y: auto; font-family: 'Consolas', 'Courier New', monospace; font-size: 0.9rem; color: #50fa7b; box-shadow: inset 0 0 10px rgba(0,0,0,0.8); }
        .console-window p { margin-bottom: 5px; line-height: 1.4; word-break: break-all; }
        .console-window .cmd-sent { color: #bd93f9; font-weight: bold; }
        .console-window .cmd-error { color: #ff5555; }
        .console-window .cmd-info { color: #8be9fd; }
        
        .status-badge { padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; display: inline-block; margin-top: 5px; }
        .status-checking { background: #f1fa8c; color: #282a36; }
        .status-online { background: #50fa7b; color: #282a36; }
        .status-offline { background: #ff5555; color: white; }

        .led { width: 14px; height: 14px; border-radius: 50%; display: inline-block; vertical-align: middle; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6); }
        .led-gray { background-color: #6272a4; }
        .led-green { background-color: #50fa7b; box-shadow: 0 0 8px #50fa7b, inset 0 1px 3px rgba(0,0,0,0.4); }
        .led-red { background-color: #ff5555; box-shadow: 0 0 8px #ff5555, inset 0 1px 3px rgba(0,0,0,0.4); }

        /* Modais */
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.8); backdrop-filter: blur(5px); }
        .modal-content { background-color: #282a36; margin: 5% auto; padding: 20px; border: 1px solid #bd93f9; border-radius: 10px; width: 90%; max-width: 1200px; box-shadow: 0 5px 25px rgba(0,0,0,0.5); text-align: center; }
        .modal-content-small { max-width: 700px; }
        .close-modal { color: #f8f8f2; float: right; font-size: 28px; font-weight: bold; cursor: pointer; }
        .close-modal:hover { color: #ff5555; }
        #screenshotImage { max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 0 15px rgba(0,0,0,0.5); margin-top: 15px; }
        
        .mosaic-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-top: 20px; }
        .mosaic-grid .btn { font-size: 0.85rem; padding: 1rem 0.5rem; text-transform: capitalize; }

        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #282a36; }
        ::-webkit-scrollbar-thumb { background: #6272a4; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #bd93f9; }
    </style>
</head>
<body>

    <div class="header">
        <h1><i class="fas fa-terminal"></i> Gestor de Terminais SSH</h1>
        <div style="color: #6272a4;">
            <span id="sys_status"><i class="fas fa-circle-notch fa-spin"></i> A verificar Paramiko...</span>
        </div>
    </div>

    <div class="container">
        <div class="panel">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; border-bottom: 1px solid #44475a; padding-bottom: 0.5rem;">
                <h3 style="margin: 0; border: none; padding: 0;"><i class="fas fa-plus-circle"></i> Adicionar Terminal</h3>
                <button class="btn btn-warning" style="width: auto; padding: 0.5rem 0.8rem; background: #f1fa8c; color: #282a36;" onclick="scanNetwork()" id="btn_scan" title="Rastrear rede local"><i class="fas fa-broadcast-tower"></i> Auto-Scan</button>
            </div>
            
            <div id="scan_results" style="display: none; margin-bottom: 1.5rem; background: #1e1e2f; padding: 10px; border-radius: 6px; border: 1px dashed #f1fa8c;">
                <h4 style="color: #f1fa8c; margin-bottom: 10px; font-size: 0.9rem;"><i class="fas fa-network-wired"></i> Terminais SSH Encontrados</h4>
                <div id="scan_list" style="display: flex; flex-direction: column; gap: 5px; max-height: 180px; overflow-y: auto;"></div>
            </div>

            <form id="addTerminalForm">
                <div class="form-group"><label>Identificação / Local (Ex: Linha 21 - Fundo)</label><input type="text" id="term_name" class="form-control" required placeholder="Nome descritivo"></div>
                <div class="form-group"><label>Endereço IP</label><input type="text" id="term_ip" class="form-control" required placeholder="192.168.1.50" pattern="^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$" title="Introduza um endereço IPv4 válido"></div>
                <div class="form-group"><label>Utilizador SSH</label><input type="text" id="term_user" class="form-control" required placeholder="pi"></div>
                <div class="form-group"><label>Password SSH</label><input type="password" id="term_pwd" class="form-control" required placeholder="••••••••"></div>
                <button type="submit" class="btn btn-success" id="btn_add"><i class="fas fa-save"></i> Guardar Configuração</button>
            </form>
            
            <div style="margin-top: 20px;">
                <p style="font-size: 0.85rem; color: #6272a4; line-height: 1.5;">
                    <i class="fas fa-info-circle"></i> O sistema guarda as passwords de forma local utilizando codificação base64 simples. Este painel destina-se a uso em redes industriais internas seguras.
                </p>
            </div>

            <div style="margin-top: 20px; border-top: 1px solid #44475a; padding-top: 20px;">
                <h4 style="color: #ff5555; margin-bottom: 10px; font-size: 0.9rem;"><i class="fas fa-exclamation-triangle"></i> Ações Globais</h4>
                <div class="btn-group">
                    <button class="btn btn-warning" style="color: #282a36;" onclick="broadcastAction('reboot')"><i class="fas fa-sync"></i> Reboot Todos</button>
                    <button class="btn btn-danger" onclick="broadcastAction('shutdown')"><i class="fas fa-power-off"></i> Desligar Todos</button>
                </div>
            </div>
        </div>

        <div>
            <div class="panel">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; border-bottom: 1px solid #44475a; padding-bottom: 0.5rem;">
                    <h3 style="margin: 0; border: none; padding: 0;"><i class="fas fa-server"></i> Terminais Conhecidos</h3>
                    <button class="btn btn-info" style="width: auto; padding: 0.5rem 1rem;" onclick="loadTerminals()"><i class="fas fa-sync-alt"></i> Atualizar Estado</button>
                </div>
                <div class="terminal-list" id="terminalListArea">
                    <div style="text-align: center; color: #6272a4; padding: 2rem;">A carregar terminais...</div>
                </div>
            </div>

            <div class="panel" id="configCard" style="display: none; margin-top: 2rem; margin-bottom: 2rem; border-left: 4px solid #f1fa8c;">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #44475a; padding-bottom: 0.5rem; margin-bottom: 1rem;">
                    <h3 style="margin: 0; border: none; padding: 0; color: #f1fa8c;"><i class="fas fa-sliders-h"></i> Configuração Remota: <span id="cfg_ip_title" style="color:#ff79c6;"></span></h3>
                    <button class="btn btn-danger" style="width: auto; padding: 0.3rem 0.8rem;" onclick="document.getElementById('configCard').style.display='none'"><i class="fas fa-times"></i> Fechar</button>
                </div>
                
                <input type="hidden" id="cfg_current_ip">

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;">
                    
                    <div>
                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-desktop"></i> Acões Rápidas (Ecrã)</h4>
                        <div class="btn-group">
                            <button class="btn btn-info cfg-btn" onclick="applyRemoteConfig('action', 'f5')"><i class="fas fa-sync"></i> F5</button>
                            <button class="btn btn-warning cfg-btn" style="color:#282a36;" onclick="applyRemoteConfig('action', 'clearcache')"><i class="fas fa-trash-alt"></i> Cache</button>
                            <button class="btn btn-secondary cfg-btn" onclick="applyRemoteConfig('action', 'hidecursor')"><i class="fas fa-mouse-pointer"></i> Rato</button>
                            <button class="btn btn-primary cfg-btn" onclick="takeScreenshot()"><i class="fas fa-camera"></i> Print</button>
                        </div>

                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-search"></i> Controlo de Zoom do Browser</h4>
                        <div class="btn-group">
                            <button class="btn btn-secondary cfg-btn" onclick="applyRemoteConfig('action', 'zoom_out')"><i class="fas fa-search-minus"></i> - Zoom</button>
                            <button class="btn btn-secondary cfg-btn" onclick="applyRemoteConfig('action', 'zoom_reset')"><i class="fas fa-compress"></i> 100%</button>
                            <button class="btn btn-secondary cfg-btn" onclick="applyRemoteConfig('action', 'zoom_in')"><i class="fas fa-search-plus"></i> + Zoom</button>
                        </div>

                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-sync-alt"></i> Rotação do Monitor Físico</h4>
                        <div class="btn-group">
                            <button class="btn btn-info cfg-btn" id="btn_rot_0" onclick="applyRemoteConfig('rot', '0')">0º</button>
                            <button class="btn btn-info cfg-btn" id="btn_rot_90" onclick="applyRemoteConfig('rot', '90')">90º</button>
                            <button class="btn btn-info cfg-btn" id="btn_rot_180" onclick="applyRemoteConfig('rot', '180')">180º</button>
                            <button class="btn btn-info cfg-btn" id="btn_rot_270" onclick="applyRemoteConfig('rot', '270')">270º</button>
                        </div>

                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-clock"></i> Relógio e Sincronização</h4>
                        <div class="btn-group">
                            <button class="btn btn-info cfg-btn" onclick="syncTime()"><i class="fas fa-sync"></i> Acertar Hora pelo meu PC</button>
                        </div>
                        <div class="input-btn-group">
                            <input type="text" id="cfg_ntp_input" class="form-control" placeholder="ex: pool.ntp.org">
                            <button class="btn btn-warning cfg-btn" onclick="applyRemoteConfig('ntp', document.getElementById('cfg_ntp_input').value)"><i class="fas fa-server"></i> NTP</button>
                        </div>
                    </div>

                    <div>
                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-id-badge"></i> Identidade do Equipamento</h4>
                        <div class="input-btn-group">
                            <input type="text" id="cfg_hostname_input" class="form-control" placeholder="A ler hostname...">
                            <button class="btn btn-warning cfg-btn" onclick="applyRemoteConfig('hostname', document.getElementById('cfg_hostname_input').value)"><i class="fas fa-save"></i> Gravar</button>
                        </div>
                        
                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-bolt"></i> Proteção de Energia e SD</h4>
                        <div class="btn-group" style="margin-bottom: 5px;">
                            <button class="btn btn-info cfg-btn" id="btn_screensaver" onclick="applyRemoteConfig('screensaver', 'off')"><i class="fas fa-eye"></i> Manter Ecrã Sempre Ligado</button>
                        </div>
                        <div class="btn-group">
                            <button class="btn btn-secondary cfg-btn" id="btn_ro_on" onclick="applyRemoteConfig('readonly', 'on')" title="Evita corromper cartão se faltar a luz"><i class="fas fa-lock"></i> Ligar Read-Only</button>
                            <button class="btn btn-danger cfg-btn" id="btn_ro_off" onclick="applyRemoteConfig('readonly', 'off')" title="Permite fazer atualizações"><i class="fas fa-unlock"></i> Desligar Read-Only</button>
                        </div>

                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px; display: flex; align-items: center; margin-top:15px;">
                            <i class="fas fa-link"></i>&nbsp;URL & Conexão do FullPageOS
                            <span id="url_led" class="led led-gray" style="margin-left: auto; margin-right: 5px;" title="A testar acessibilidade do URL..."></span>
                            <small id="url_status_text" style="font-size: 0.75rem; color: #6272a4;">A ler...</small>
                        </h4>
                        <div class="input-btn-group">
                            <input type="text" id="cfg_url_input" class="form-control" placeholder="http://192.168.x.x:5000">
                            <button class="btn btn-primary cfg-btn" title="Lista de Mosaicos Automática" onclick="openMosaicSelector()"><i class="fas fa-list"></i> Lista</button>
                            <button class="btn btn-success cfg-btn" onclick="applyRemoteConfig('url', document.getElementById('cfg_url_input').value)"><i class="fas fa-check"></i> Enviar</button>
                        </div>
                        <div class="btn-group">
                            <button class="btn btn-warning cfg-btn" id="btn_auto_reconnect" onclick="applyRemoteConfig('autoreconnect', 'on')"><i class="fas fa-wifi"></i> Instalar/Ativar Auto-Reconexão (F5)</button>
                        </div>

                        <h4 style="color: #f8f8f2; margin-bottom: 10px; font-size: 1rem; border-bottom: 1px solid #44475a; padding-bottom: 5px;"><i class="fas fa-network-wired"></i> Configuração de IP e Wi-Fi</h4>
                        <div class="btn-group">
                            <button class="btn btn-info cfg-btn" id="btn_net_dhcp" onclick="applyRemoteConfig('net', 'dhcp')">Cabo: Auto</button>
                            <button class="btn btn-info cfg-btn" id="btn_net_static" onclick="toggleStaticForm()">Cabo: IP Fixo</button>
                        </div>

                        <div id="static_ip_form" style="display: none; background: #1e1e2f; padding: 10px; border-radius: 6px; border: 1px solid #6272a4; margin-bottom: 10px;">
                            <div class="form-group"><label>Novo IP Fixo (ex: 192.168.1.100/24)</label><input type="text" id="cfg_static_ip" class="form-control" placeholder="192.168.1.100/24"></div>
                            <div class="form-group"><label>Gateway (Router)</label><input type="text" id="cfg_static_gw" class="form-control" placeholder="192.168.1.1"></div>
                            <div class="form-group"><label>DNS (ex: 8.8.8.8)</label><input type="text" id="cfg_static_dns" class="form-control" placeholder="8.8.8.8"></div>
                            <button class="btn btn-warning" onclick="applyRemoteConfig('net', 'static')"><i class="fas fa-paper-plane"></i> Validar e Enviar IP Fixo</button>
                        </div>

                        <div id="wifi_form" style="background: #1e1e2f; padding: 10px; border-radius: 6px; border: 1px solid #6272a4;">
                            <h5 style="color: #f8f8f2; margin-top: 0; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
                                <span><i class="fas fa-wifi"></i> Ligar a Rede Wi-Fi</span>
                                <button class="btn btn-info" style="width: auto; padding: 0.2rem 0.5rem; font-size: 0.8rem;" onclick="scanWifiNetworks()"><i class="fas fa-search"></i> Procurar</button>
                            </h5>
                            <div id="wifi_scan_results" style="display: none; margin-bottom: 10px; max-height: 150px; overflow-y: auto; background: #282a36; border-radius: 4px; padding: 5px;"></div>
                            
                            <div class="form-group"><label>Nome da Rede (SSID)</label><input type="text" id="cfg_wifi_ssid" class="form-control" placeholder="Minha_Rede_Industrial"></div>
                            <div class="form-group"><label>Password do Wi-Fi</label><input type="password" id="cfg_wifi_pwd" class="form-control" placeholder="••••••••"></div>
                            <button class="btn btn-warning" onclick="applyRemoteConfig('net', 'wifi')"><i class="fas fa-broadcast-tower"></i> Conectar ao Wi-Fi</button>
                        </div>

                    </div>
                </div>
            </div>

            <div class="panel" style="margin-top: 2rem;">
                <h3><i class="fas fa-laptop-code"></i> Saída de Consola SSH</h3>
                <div class="console-window" id="consoleOutput">
                    <p class="cmd-info">Console SSH Inicializada. Selecione uma ação nos terminais ou envie um comando livre abaixo.</p>
                </div>
                
                <div style="margin-top: 10px; display: flex; gap: 10px; align-items: center; background: #1e1e2f; padding: 10px; border-radius: 6px; border: 1px solid #44475a;">
                    <select id="console_target_ip" class="form-control" style="width: 250px; background: #282a36;">
                        <option value="">-- Selecione o Terminal --</option>
                    </select>
                    <input type="text" id="console_custom_cmd" class="form-control" placeholder="Escreva o comando bash (ex: ls -la /boot)" style="flex: 1;" onkeypress="if(event.key === 'Enter') sendCustomCommand()">
                    <button class="btn btn-warning" style="width: auto;" onclick="sendCustomCommand()"><i class="fas fa-paper-plane"></i> Enviar</button>
                    <button class="btn btn-info" style="width: auto;" onclick="document.getElementById('consoleOutput').innerHTML = ''"><i class="fas fa-eraser"></i> Limpar</button>
                </div>
            </div>
        </div>
    </div>

    <div id="screenshotModal" class="modal">
        <div class="modal-content">
            <span class="close-modal" onclick="document.getElementById('screenshotModal').style.display='none'">&times;</span>
            <h3 style="color: #bd93f9; margin-top: 0;"><i class="fas fa-camera"></i> Visão Atual do Ecrã</h3>
            <p id="screenshotLoading" style="color: #f1fa8c;"><i class="fas fa-spinner fa-spin"></i> A tirar fotografia... A aguardar transferência SSH...</p>
            <img id="screenshotImage" src="" style="display: none;">
        </div>
    </div>

    <div id="mosaicModal" class="modal">
        <div class="modal-content modal-content-small">
            <span class="close-modal" onclick="document.getElementById('mosaicModal').style.display='none'">&times;</span>
            <h3 style="color: #8be9fd; margin-top: 0; text-align: left;"><i class="fas fa-th"></i> Selecionar Mosaico (Lido do JSON)</h3>
            <p style="color: #6272a4; text-align: left; margin-bottom: 15px;">A ler configurações de sistema ativas. A URL será preenchida automaticamente.</p>
            <div class="mosaic-grid" id="mosaic_grid_area">
                </div>
        </div>
    </div>

    <div id="hwModal" class="modal">
        <div class="modal-content modal-content-small">
            <span class="close-modal" onclick="document.getElementById('hwModal').style.display='none'">&times;</span>
            <h3 style="color: #8be9fd; margin-top: 0; text-align: left;"><i class="fas fa-microchip"></i> Diagnóstico de Hardware: <span id="hw_modal_title" style="color:#f8f8f2;"></span></h3>
            
            <div id="hw_loading" style="color: #f1fa8c; padding: 20px; text-align: center;"><i class="fas fa-spinner fa-spin"></i> A ler sensores do Raspberry Pi via SSH...</div>
            
            <div id="hw_data_grid" style="display: none; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px; text-align: left;">
                <div class="panel" style="padding: 1rem; background: #383a59; border: 1px solid #6272a4;">
                    <h4 style="color: #ff79c6; margin-bottom: 5px;"><i class="fas fa-thermometer-half"></i> Temperatura do CPU</h4>
                    <p id="hw_temp" style="font-size: 1.3rem; font-weight: bold;"></p>
                </div>
                <div class="panel" style="padding: 1rem; background: #383a59; border: 1px solid #6272a4;">
                    <h4 style="color: #50fa7b; margin-bottom: 5px;"><i class="fas fa-microchip"></i> Carga (Load Avg)</h4>
                    <p id="hw_cpu" style="font-size: 1.3rem; font-weight: bold;"></p>
                </div>
                <div class="panel" style="padding: 1rem; background: #383a59; border: 1px solid #6272a4;">
                    <h4 style="color: #8be9fd; margin-bottom: 5px;"><i class="fas fa-memory"></i> Memória RAM</h4>
                    <p id="hw_ram" style="font-size: 1.2rem; font-weight: bold;"></p>
                </div>
                <div class="panel" style="padding: 1rem; background: #383a59; border: 1px solid #6272a4;">
                    <h4 style="color: #ffb86c; margin-bottom: 5px;"><i class="fas fa-hdd"></i> Disco (Cartão SD)</h4>
                    <p id="hw_disk" style="font-size: 1.2rem; font-weight: bold;"></p>
                </div>
                <div class="panel" style="grid-column: 1 / -1; padding: 1rem; background: #383a59; border: 1px solid #6272a4;">
                    <h4 style="color: #f8f8f2; margin-bottom: 5px;"><i class="fas fa-clock"></i> Tempo Ligado (Uptime Total)</h4>
                    <p id="hw_uptime" style="font-size: 1.2rem; font-weight: bold;"></p>
                </div>
            </div>
        </div>
    </div>

    <script>
        let lastUptimeFetch = {}; 

        // Adiciona a animação de brilho (shimmer) aos botões ao clicar
        document.addEventListener('click', function(e) {
            const btn = e.target.closest('.btn');
            if (btn) {
                btn.classList.remove('shimmer-effect');
                void btn.offsetWidth; // Força reflow
                btn.classList.add('shimmer-effect');
                setTimeout(() => btn.classList.remove('shimmer-effect'), 500);
            }
        });

        function logToConsole(message, type = 'info') {
            const consoleWin = document.getElementById('consoleOutput');
            const p = document.createElement('p');
            let prefix = '';
            if(type === 'sent') { p.className = 'cmd-sent'; prefix = 'root@painel:~$ '; }
            else if(type === 'error') { p.className = 'cmd-error'; prefix = '[ERRO] '; }
            else if(type === 'response') { p.style.color = '#f8f8f2'; prefix = '>> '; }
            else { p.className = 'cmd-info'; prefix = '[INFO] '; }
            
            p.innerText = prefix + message;
            consoleWin.appendChild(p);
            consoleWin.scrollTop = consoleWin.scrollHeight;
        }

        function scanNetwork() {
            const btn = document.getElementById('btn_scan');
            const resDiv = document.getElementById('scan_results');
            const listDiv = document.getElementById('scan_list');
            
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> A procurar...';
            btn.disabled = true;
            resDiv.style.display = 'block';
            listDiv.innerHTML = '<span style="color: #6272a4; font-size: 0.85rem;">A rastrear a sub-rede via Multi-Threading (porta 22)... Aguarde.</span>';
            
            fetch('/api/terminals/scan')
            .then(r => r.json())
            .then(data => {
                listDiv.innerHTML = '';
                if(data.ips && data.ips.length > 0) {
                    data.ips.forEach(ip => {
                        const div = document.createElement('div');
                        div.style.display = 'flex'; div.style.justifyContent = 'space-between'; div.style.alignItems = 'center';
                        div.style.background = '#282a36'; div.style.padding = '5px 10px'; div.style.borderRadius = '4px';
                        div.innerHTML = `
                            <span style="color: #50fa7b; font-family: monospace;">${ip}</span>
                            <button type="button" class="btn btn-info" style="padding: 0.2rem 0.5rem; font-size: 0.8rem; width: auto;" onclick="autofillForm('${ip}')">Usar IP</button>
                        `;
                        listDiv.appendChild(div);
                    });
                } else {
                    listDiv.innerHTML = '<span style="color: #ff5555; font-size: 0.85rem;">Nenhum dispositivo encontrado na rede local.</span>';
                }
            })
            .catch(err => { listDiv.innerHTML = `<span style="color: #ff5555; font-size: 0.85rem;">Erro na busca: ${err.message}</span>`; })
            .finally(() => { btn.innerHTML = '<i class="fas fa-broadcast-tower"></i> Auto-Scan'; btn.disabled = false; });
        }

        function autofillForm(ip) {
            document.getElementById('term_ip').value = ip;
            document.getElementById('term_user').value = 'pi';
            document.getElementById('term_name').focus();
        }

        function loadTerminals() {
            const listArea = document.getElementById('terminalListArea');
            listArea.innerHTML = '<div style="text-align: center; color: #6272a4; padding: 2rem;"><i class="fas fa-spinner fa-spin"></i> A ler base de dados...</div>';
            
            fetch('/api/terminals')
                .then(r => r.json())
                .then(data => {
                    if(!data.paramiko) {
                        document.getElementById('sys_status').innerHTML = '<i class="fas fa-exclamation-triangle" style="color: #ff5555;"></i> Faltam Dependências (Ver Consola)';
                        logToConsole("ATENÇÃO: A biblioteca 'paramiko' não está instalada no servidor.", "error");
                        logToConsole("Para que as ligações SSH funcionem corretamente, instale a biblioteca via terminal da NAS: pip install paramiko", "response");
                    } else {
                        document.getElementById('sys_status').innerHTML = '<i class="fas fa-check-circle" style="color: #50fa7b;"></i> Sistema Operacional SSH Pronto';
                    }

                    if(Object.keys(data.terminals).length === 0) {
                        listArea.innerHTML = '<div style="text-align: center; color: #6272a4; padding: 2rem;">Nenhum terminal registado. Adicione um terminal no formulário à esquerda.</div>';
                        document.getElementById('console_target_ip').innerHTML = '<option value="">-- Sem Terminais --</option>';
                        return;
                    }
                    
                    listArea.innerHTML = '';
                    const targetSelect = document.getElementById('console_target_ip');
                    targetSelect.innerHTML = '<option value="">-- Selecione o Terminal --</option>';
                    
                    const termArray = Object.keys(data.terminals).map(ip => { return { ip: ip, ...data.terminals[ip] }; });
                    termArray.sort((a, b) => a.name.localeCompare(b.name));
                    
                    termArray.forEach(term => {
                        const card = document.createElement('div');
                        card.className = 'terminal-card';
                        const ipSafe = term.ip.replace(/\./g, '_');
                        card.innerHTML = `
                            <div class="terminal-info">
                                <h4>${term.name}</h4>
                                <p><i class="fas fa-network-wired" style="color: #6272a4;"></i> ${term.ip}</p>
                                <small><i class="fas fa-user" style="color: #6272a4;"></i> ${term.username}</small><br>
                                <small id="uptime_${ipSafe}" style="color: #bd93f9; font-weight: bold;"><i class="fas fa-clock"></i> A ler tempo de atividade...</small><br>
                                <span class="status-badge status-checking" id="status_${ipSafe}">A verificar...</span>
                            </div>
                            <div class="terminal-actions">
                                <button class="btn btn-secondary" style="background: #bd93f9; color: #f8f8f2;" title="Diagnóstico de Hardware" onclick="openHwStatus('${term.ip}', '${term.name}')"><i class="fas fa-microchip"></i> Status HW</button>
                                <button class="btn btn-info" style="background: #f1fa8c; color: #282a36;" title="Configurar Sistema Operativo" onclick="openConfigCard('${term.ip}')"><i class="fas fa-cog"></i> Configurar</button>
                                <button class="btn btn-info" title="Testar Ligação Ping" onclick="sendCommand('${term.ip}', 'ping')"><i class="fas fa-plug"></i> Ping</button>
                                <button class="btn btn-danger" style="background: #ffb86c; color: #282a36;" title="Reiniciar Raspberry Pi" onclick="sendCommand('${term.ip}', 'reboot')"><i class="fas fa-power-off"></i> Reboot</button>
                                <button class="btn btn-danger" title="Apagar do Painel" onclick="deleteTerminal('${term.ip}')"><i class="fas fa-trash"></i></button>
                            </div>
                        `;
                        listArea.appendChild(card);
                        checkStatus(term.ip);
                        
                        targetSelect.innerHTML += `<option value="${term.ip}">${term.name} (${term.ip})</option>`;
                    });
                })
                .catch(err => { listArea.innerHTML = `<div style="color: #ff5555; padding: 1rem;">Erro de rede: ${err.message}</div>`; });
        }

        function fetchUptime(ip, badgeId) {
            const uptimeEl = document.getElementById('uptime_' + badgeId);
            if(!uptimeEl) return;
            fetch('/api/terminals/uptime', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r=>r.json()).then(d => {
                if(d.status === 'success' && d.uptime) uptimeEl.innerHTML = '<i class="fas fa-clock"></i> Ligado há: ' + d.uptime;
                else uptimeEl.innerHTML = '<i class="fas fa-clock"></i> Falha ao ler Uptime';
            }).catch(() => { uptimeEl.innerHTML = '<i class="fas fa-clock"></i> Erro de rede SSH'; });
        }

        function checkStatus(ip) {
            const badgeId = ip.replace(/\./g, '_');
            fetch('/api/terminals/ping', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r => r.json()).then(data => {
                const badge = document.getElementById('status_' + badgeId);
                const uptimeEl = document.getElementById('uptime_' + badgeId);
                if(badge) {
                    if(data.status === 'online') {
                        badge.className = 'status-badge status-online'; badge.innerText = 'ONLINE';
                        const now = Date.now();
                        if(!lastUptimeFetch[ip] || now - lastUptimeFetch[ip] > 30000) { lastUptimeFetch[ip] = now; fetchUptime(ip, badgeId); }
                    } else {
                        badge.className = 'status-badge status-offline'; badge.innerText = 'OFFLINE';
                        if(uptimeEl) uptimeEl.innerHTML = '<i class="fas fa-clock"></i> Terminal Desligado';
                        lastUptimeFetch[ip] = 0;
                    }
                }
            }).catch(e => {
                const badge = document.getElementById('status_' + badgeId);
                if(badge) { badge.className = 'status-badge status-offline'; badge.innerText = 'ERRO'; }
            });
        }

        document.getElementById('addTerminalForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const btn = document.getElementById('btn_add');
            btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> A gravar...';
            const data = {
                name: document.getElementById('term_name').value, ip: document.getElementById('term_ip').value,
                username: document.getElementById('term_user').value, password: document.getElementById('term_pwd').value
            };
            fetch('/api/terminals/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') { logToConsole(`Terminal ${data.ip} (${data.name}) adicionado com sucesso.`, "info"); this.reset(); loadTerminals(); } 
                else { logToConsole(`Erro ao adicionar terminal: ${res.message}`, "error"); }
            }).catch(err => logToConsole(`Falha na rede: ${err.message}`, "error")).finally(() => {
                btn.disabled = false; btn.innerHTML = '<i class="fas fa-save"></i> Guardar Configuração';
            });
        });

        function deleteTerminal(ip) {
            if(confirm(`Tem a certeza absoluta que deseja remover o terminal ${ip} da lista?`)) {
                fetch('/api/terminals/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
                }).then(r => r.json()).then(res => {
                    if(res.status === 'success') { logToConsole(`Terminal ${ip} apagado da base de dados.`, "info"); loadTerminals(); } 
                    else { logToConsole(`Falha ao apagar terminal: ${res.message}`, "error"); }
                });
            }
        }

        function sendCommand(ip, action) {
            let command = "";
            let confirmMsg = "";
            if(action === 'ping') command = "echo 'Conexão SSH estabelecida e ativa!'";
            else if (action === 'reboot') {
                confirmMsg = `ATENÇÃO: Vai reiniciar forçadamente o equipamento no IP ${ip}.\nDeseja prosseguir?`;
                command = "sudo reboot";
            }
            if(confirmMsg && !confirm(confirmMsg)) { logToConsole(`Ação '${action}' no IP ${ip} cancelada.`, "info"); return; }
            logToConsole(`[${ip}] A estabelecer ponte SSH e enviar comando...`, "sent");
            
            fetch('/api/terminals/execute', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip, command: command})
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    if(res.output) res.output.replace(/\n$/, "").split('\n').forEach(line => logToConsole(line, "response"));
                    else logToConsole("Comando enviado com sucesso.", "response");
                    if(action === 'reboot') { logToConsole(`A aguardar desconexão do IP ${ip}...`, "info"); setTimeout(() => checkStatus(ip), 5000); }
                } else { logToConsole(`[Falha SSH] ${res.message}`, "error"); }
            }).catch(err => logToConsole(`Falha drástica na rede: ${err.message}`, "error"));
        }

        function sendCustomCommand() {
            const ip = document.getElementById('console_target_ip').value;
            const cmdInput = document.getElementById('console_custom_cmd');
            const command = cmdInput.value.trim();

            if(!ip) { alert('Por favor, selecione um Terminal na lista ao lado da caixa de texto.'); return; }
            if(!command) return;

            logToConsole(`[${ip}] A enviar comando manual: ${command}`, "sent");
            cmdInput.value = '';

            fetch('/api/terminals/execute', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip, command: command})
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    if(res.output) res.output.replace(/\n$/, "").split('\n').forEach(line => logToConsole(line, "response"));
                    else logToConsole("Comando processado sem saída visual de retorno.", "response");
                } else {
                    logToConsole(`[ERRO SSH] ${res.message}`, "error");
                }
            }).catch(err => logToConsole(`Falha de rede ao enviar o comando: ${err.message}`, "error"));
        }

        function broadcastAction(action) {
            let msg = action === 'reboot' ? 'Vai reiniciar todos os terminais em simultâneo. Confirma?' : 'Vai DESLIGAR todos os terminais em simultâneo. Terá de os ligar fisicamente à corrente depois. Confirma?';
            if(!confirm(msg)) return;
            
            logToConsole(`A enviar comando global (${action}) para todos os terminais ativos... Isto pode demorar.`, 'sent');
            
            fetch('/api/terminals/broadcast', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: action})
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    logToConsole(`Ordem global enviada com sucesso para os terminais em rede.`, 'response');
                    setTimeout(loadTerminals, 10000);
                } else {
                    logToConsole(`Erro no broadcast: ${res.message}`, 'error');
                }
            }).catch(err => logToConsole(`Falha de rede no broadcast: ${err.message}`, "error"));
        }

        function scanWifiNetworks() {
            const ip = document.getElementById('cfg_current_ip').value;
            const resDiv = document.getElementById('wifi_scan_results');
            
            resDiv.style.display = 'block';
            resDiv.innerHTML = '<div style="text-align: center; color: #f1fa8c; padding: 10px;"><i class="fas fa-spinner fa-spin"></i> A procurar redes Wi-Fi locais via SSH...</div>';
            
            fetch('/api/terminals/scan_wifi', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    if(res.networks.length === 0) {
                        resDiv.innerHTML = '<div style="color: #ff5555; padding: 5px; text-align:center;">Nenhuma rede encontrada. A antena Wi-Fi está ligada?</div>';
                        return;
                    }
                    resDiv.innerHTML = '';
                    res.networks.forEach(net => {
                        let signalColor = '#50fa7b'; // green
                        if(parseInt(net.signal) < 60) signalColor = '#f1fa8c'; // yellow
                        if(parseInt(net.signal) < 30) signalColor = '#ff5555'; // red
                        
                        resDiv.innerHTML += `
                            <div style="display: flex; justify-content: space-between; align-items: center; padding: 5px; border-bottom: 1px solid #44475a; cursor: pointer; transition: background 0.2s;" onmouseover="this.style.background='#44475a'" onmouseout="this.style.background='transparent'" onclick="selectWifi('${net.ssid}')">
                                <span style="color: #f8f8f2; font-weight: bold;">${net.ssid}</span>
                                <span style="color: ${signalColor};"><i class="fas fa-signal"></i> ${net.signal}%</span>
                            </div>
                        `;
                    });
                } else {
                    resDiv.innerHTML = `<div style="color: #ff5555; padding: 5px; font-size: 0.85rem;">Erro: ${res.message}</div>`;
                }
            }).catch(err => {
                resDiv.innerHTML = `<div style="color: #ff5555; padding: 5px; font-size: 0.85rem;">Falha de rede: ${err.message}</div>`;
            });
        }

        function selectWifi(ssid) {
            document.getElementById('cfg_wifi_ssid').value = ssid;
            document.getElementById('wifi_scan_results').style.display = 'none';
            document.getElementById('cfg_wifi_pwd').focus();
        }

        function openMosaicSelector() {
            document.getElementById('mosaicModal').style.display = 'block';
            const grid = document.getElementById('mosaic_grid_area');
            grid.innerHTML = '<div style="text-align: center; color: #f1fa8c; grid-column: 1 / -1;"><i class="fas fa-spinner fa-spin"></i> A ler portas do JSON de configuração na NAS...</div>';

            fetch('/api/mosaics').then(r=>r.json()).then(res => {
                grid.innerHTML = '';
                if(res.status === 'success' && res.mosaics.length > 0) {
                    res.mosaics.sort((a,b) => a.port - b.port);
                    res.mosaics.forEach(m => {
                        let btnClass = 'btn-info';
                        if(m.name.toLowerCase().includes('global')) btnClass = 'btn-primary';
                        else if(m.port % 2 === 0) btnClass = 'btn-secondary'; 
                        
                        grid.innerHTML += `<button class="btn ${btnClass}" onclick="fillMosaicUrl(${m.port})"><i class="fas fa-desktop"></i> ${m.name} (${m.port})</button>`;
                    });
                } else {
                    grid.innerHTML = '<div style="color: #ff5555; grid-column: 1 / -1; text-align: center;">Aviso: O ficheiro JSON geral das linhas não foi encontrado ou está vazio.<br>A carregar lista por defeito...</div>';
                    setTimeout(() => {
                        grid.innerHTML = `
                            <button class="btn btn-primary" onclick="fillMosaicUrl(5098)"><i class="fas fa-globe"></i> Global Lateral</button>
                            <button class="btn btn-primary" onclick="fillMosaicUrl(5099)"><i class="fas fa-globe"></i> Global Fundo</button>
                            <div style="grid-column: 1 / -1; border-top: 1px solid #44475a; margin: 10px 0;"></div>
                            <button class="btn btn-info" onclick="fillMosaicUrl(5001)">L21 Lateral</button>
                            <button class="btn btn-secondary" onclick="fillMosaicUrl(5002)">L21 Fundo</button>
                            <button class="btn btn-info" onclick="fillMosaicUrl(5003)">L22 Lateral</button>
                            <button class="btn btn-secondary" onclick="fillMosaicUrl(5004)">L22 Fundo</button>
                            <button class="btn btn-info" onclick="fillMosaicUrl(5005)">L23 Lateral</button>
                            <button class="btn btn-secondary" onclick="fillMosaicUrl(5006)">L23 Fundo</button>
                        `;
                    }, 2000);
                }
            }).catch(() => {
                grid.innerHTML = '<div style="color: #ff5555; grid-column: 1 / -1; text-align: center;">Erro de rede ao ler mosaicos.</div>';
            });
        }
        
        function fillMosaicUrl(port) {
            const host = window.location.hostname;
            document.getElementById('cfg_url_input').value = `http://${host}:${port}`;
            document.getElementById('mosaicModal').style.display = 'none';
        }

        function openConfigCard(ip) {
            document.getElementById('configCard').style.display = 'block';
            document.getElementById('cfg_ip_title').innerText = ip;
            document.getElementById('cfg_current_ip').value = ip;
            document.getElementById('static_ip_form').style.display = 'none';
            document.getElementById('wifi_scan_results').style.display = 'none';
            
            document.getElementById('cfg_url_input').value = 'A ler estado atual...';
            document.getElementById('cfg_hostname_input').value = 'A ler...';
            document.getElementById('cfg_ntp_input').value = 'A ler...';
            
            const led = document.getElementById('url_led');
            const statusTxt = document.getElementById('url_status_text');
            led.className = 'led led-gray';
            statusTxt.innerText = 'A testar...';
            
            document.querySelectorAll('.cfg-btn').forEach(btn => {
                if(!btn.classList.contains('btn-warning') && !btn.classList.contains('btn-secondary')) {
                    btn.style.background = '#8be9fd'; btn.style.color = '#282a36';
                }
            });

            fetch('/api/terminals/get_real_state', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r => r.json()).then(data => {
                if(data.status === 'success') {
                    document.getElementById('cfg_url_input').value = data.real_url || '';
                    document.getElementById('cfg_hostname_input').value = data.real_hostname || '';
                    document.getElementById('cfg_ntp_input').value = data.real_ntp || '';
                    
                    // Gestão do LED do URL
                    const httpCode = parseInt(data.real_http);
                    if(httpCode >= 200 && httpCode < 400) {
                        led.className = 'led led-green';
                        statusTxt.innerText = '(Acessível - OK)';
                        statusTxt.style.color = '#50fa7b';
                    } else if (httpCode > 0) {
                        led.className = 'led led-red';
                        statusTxt.innerText = '(Erro: HTTP ' + httpCode + ')';
                        statusTxt.style.color = '#ff5555';
                    } else {
                        led.className = 'led led-red';
                        statusTxt.innerText = '(Inacessível / Timeout)';
                        statusTxt.style.color = '#ff5555';
                    }
                    
                    // Tratamento do Modo Read-Only
                    if(data.real_ro === 'ON') {
                        document.getElementById('btn_ro_on').style.background = '#50fa7b';
                        document.getElementById('btn_ro_on').style.color = '#282a36';
                        document.getElementById('btn_ro_off').style.background = '#ff5555';
                        document.getElementById('btn_ro_off').style.color = '#f8f8f2';
                    } else if (data.real_ro === 'OFF') {
                        document.getElementById('btn_ro_on').style.background = '#6272a4';
                        document.getElementById('btn_ro_on').style.color = '#f8f8f2';
                        document.getElementById('btn_ro_off').style.background = '#50fa7b';
                        document.getElementById('btn_ro_off').style.color = '#282a36';
                    }
                    
                    const cfg = data.config || {};
                    if(cfg.rotation !== undefined && document.getElementById('btn_rot_' + cfg.rotation)) document.getElementById('btn_rot_' + cfg.rotation).style.background = '#50fa7b';
                    if(cfg.screensaver === 'off') document.getElementById('btn_screensaver').style.background = '#50fa7b';
                    if(cfg.network === 'dhcp') document.getElementById('btn_net_dhcp').style.background = '#50fa7b';
                    if(cfg.network === 'static') document.getElementById('btn_net_static').style.background = '#50fa7b';
                    if(cfg.auto_reconnect === 'on') {
                        document.getElementById('btn_auto_reconnect').style.background = '#50fa7b';
                        document.getElementById('btn_auto_reconnect').innerHTML = '<i class="fas fa-check-circle"></i> Auto-Reconexão Instalada e Ativa';
                    }
                } else {
                    document.getElementById('cfg_url_input').value = 'Falha ao ler terminal';
                    document.getElementById('cfg_hostname_input').value = 'Falha';
                    document.getElementById('cfg_ntp_input').value = 'Falha';
                    led.className = 'led led-red';
                    statusTxt.innerText = '(Erro de Leitura)';
                }
            }).catch(() => {
                document.getElementById('cfg_url_input').value = 'Erro de rede SSH';
            });
        }

        function openHwStatus(ip, name) {
            document.getElementById('hwModal').style.display = 'block';
            document.getElementById('hw_modal_title').innerText = name + ' (' + ip + ')';
            document.getElementById('hw_loading').style.display = 'block';
            document.getElementById('hw_data_grid').style.display = 'none';

            fetch('/api/terminals/hw_status', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r => r.json()).then(data => {
                if(data.status === 'success') {
                    document.getElementById('hw_temp').innerText = data.hw.temp;
                    document.getElementById('hw_cpu').innerText = data.hw.cpu;
                    document.getElementById('hw_ram').innerText = data.hw.ram;
                    document.getElementById('hw_disk').innerText = data.hw.disk;
                    document.getElementById('hw_uptime').innerText = data.hw.uptime;
                    
                    // Alertas de cor para temperatura
                    const tempStr = data.hw.temp;
                    const tempVal = parseFloat(tempStr.replace(/[^0-9.]/g, ''));
                    if(!isNaN(tempVal)) {
                        if(tempVal > 75) document.getElementById('hw_temp').style.color = '#ff5555';
                        else if(tempVal > 60) document.getElementById('hw_temp').style.color = '#ffb86c';
                        else document.getElementById('hw_temp').style.color = '#50fa7b';
                    } else {
                        document.getElementById('hw_temp').style.color = '#f8f8f2';
                    }

                    document.getElementById('hw_loading').style.display = 'none';
                    document.getElementById('hw_data_grid').style.display = 'grid';
                } else {
                    document.getElementById('hw_loading').innerHTML = '<span style="color:#ff5555;"><i class="fas fa-times-circle"></i> Erro: ' + data.message + '</span>';
                }
            }).catch(err => {
                document.getElementById('hw_loading').innerHTML = '<span style="color:#ff5555;"><i class="fas fa-wifi"></i> Falha de rede: ' + err.message + '</span>';
            });
        }

        function toggleStaticForm() { document.getElementById('static_ip_form').style.display = 'block'; }

        function syncTime() {
            const ip = document.getElementById('cfg_current_ip').value;
            const now = new Date();
            const pad = (n) => n < 10 ? '0' + n : n;
            const timeStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
            
            logToConsole(`[${ip}] A enviar comando para acertar data/hora: ${timeStr}...`, 'sent');
            fetch('/api/terminals/apply_config', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, 
                body: JSON.stringify({ ip: ip, type: 'time', value: timeStr })
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    logToConsole(`[${ip}] Hora do Raspberry Pi atualizada com sucesso!`, 'response');
                    checkStatus(ip);
                } else {
                    logToConsole(`[ERRO] Falha ao acertar hora: ${res.message}`, 'error');
                }
            });
        }

        function applyRemoteConfig(type, value) {
            const ip = document.getElementById('cfg_current_ip').value;
            let payload = { ip: ip, type: type, value: value };
            
            if(type === 'net' && value === 'static') {
                payload.static_ip = document.getElementById('cfg_static_ip').value;
                payload.static_gw = document.getElementById('cfg_static_gw').value;
                payload.static_dns = document.getElementById('cfg_static_dns').value;
                if(!payload.static_ip) { alert("Preencha o IP para avançar!"); return; }
                if(!confirm("Atenção: Ao enviar um IP fixo a máquina perderá a ligação atual e assumirá o novo IP imediatamente. Prosseguir?")) return;
            }
            if(type === 'net' && value === 'wifi') {
                payload.wifi_ssid = document.getElementById('cfg_wifi_ssid').value;
                payload.wifi_pwd = document.getElementById('cfg_wifi_pwd').value;
                if(!payload.wifi_ssid) { alert("Preencha o Nome da Rede (SSID)!"); return; }
                if(!confirm("Atenção: Ao ligar ao Wi-Fi, o Raspberry Pi poderá mudar de endereço IP e perder a ligação de rede atual. Prosseguir?")) return;
            }
            if(type === 'url') {
                if(!value || value === 'A ler estado atual...' || value === 'Erro de rede SSH') { alert("Por favor introduza o novo URL (ex: http://192.168.1.50:5001)"); return; }
            }
            if(type === 'ntp') {
                if(!value || value === 'A ler...' || value === 'Falha') { alert("Por favor introduza o servidor NTP (ex: pool.ntp.org)"); return; }
            }
            if(type === 'hostname') {
                if(!value || value === 'A ler...' || value === 'Falha') { alert("Por favor introduza o novo Hostname."); return; }
                if(!confirm(`Alterar a identidade da máquina para '${value}'?`)) return;
            }
            if(type === 'readonly') {
                if(value === 'on' && !confirm("Vai bloquear o cartão SD para leitura. O Raspberry Pi vai reiniciar de imediato. Deseja prosseguir?")) return;
                if(value === 'off' && !confirm("Vai desbloquear o cartão SD para permitir escritas (ex: atualizações). O Raspberry Pi vai reiniciar de imediato. Deseja prosseguir?")) return;
            }

            if(type === 'net' && value === 'static') {
                logToConsole(`[${ip}] A verificar disponibilidade do novo IP, Gateway e DNS...`, 'sent');
            } else {
                logToConsole(`[${ip}] A enviar comando de configuração especial (${type})...`, 'sent');
            }

            // Para mudança de URL, mostramos o status em loading no LED
            if(type === 'url') {
                document.getElementById('url_led').className = 'led led-gray';
                document.getElementById('url_status_text').innerText = 'A aplicar...';
                document.getElementById('url_status_text').style.color = '#6272a4';
            }

            fetch('/api/terminals/apply_config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    logToConsole(`[${ip}] Configuração (${type}) aplicada com sucesso!`, 'response');
                    if(type === 'url') logToConsole(`Atenção: A mudança de URL causa o reinicio do gestor gráfico (LightDM). O ecrã vai piscar.`, 'info');
                    if(type === 'ntp') logToConsole(`Servidor NTP atualizado. O relógio irá ajustar-se gradualmente.`, 'info');
                    if(type === 'readonly') logToConsole(`Atenção: O terminal vai perder a ligação enquanto reinicia para aplicar o bloqueio/desbloqueio do SD.`, 'info');
                    
                    // Se foi uma configuração não destrutiva, recarrega o cartão para testar o novo LED ou Status
                    if(['url', 'net', 'hostname', 'ntp'].includes(type)) {
                        setTimeout(() => openConfigCard(ip), 2000);
                    }
                    if(type === 'readonly') {
                        setTimeout(() => checkStatus(ip), 6000);
                    }
                } else {
                    logToConsole(`[ERRO] Falha ao configurar: ${res.message}`, 'error');
                    if(type === 'url') {
                        document.getElementById('url_led').className = 'led led-red';
                        document.getElementById('url_status_text').innerText = 'Falha ao gravar';
                    }
                }
            });
        }

        function takeScreenshot() {
            const ip = document.getElementById('cfg_current_ip').value;
            logToConsole(`[${ip}] A pedir captura de ecrã remota... Isto pode demorar alguns segundos.`, 'sent');
            
            document.getElementById('screenshotModal').style.display = 'block';
            document.getElementById('screenshotLoading').style.display = 'block';
            document.getElementById('screenshotImage').style.display = 'none';
            document.getElementById('screenshotImage').src = '';

            fetch('/api/terminals/screenshot', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip: ip})
            }).then(r => r.json()).then(res => {
                if(res.status === 'success') {
                    logToConsole(`[${ip}] Imagem recebida com sucesso!`, 'response');
                    document.getElementById('screenshotLoading').style.display = 'none';
                    document.getElementById('screenshotImage').style.display = 'inline-block';
                    document.getElementById('screenshotImage').src = 'data:image/png;base64,' + res.image;
                } else {
                    document.getElementById('screenshotModal').style.display = 'none';
                    logToConsole(`[ERRO] Falha ao capturar ecrã: ${res.message}`, 'error');
                    alert("Falha ao capturar o ecrã. Veja a consola para mais detalhes.");
                }
            }).catch(err => {
                document.getElementById('screenshotModal').style.display = 'none';
                logToConsole(`Falha de rede ao transferir imagem: ${err.message}`, "error");
            });
        }

        document.addEventListener('DOMContentLoaded', loadTerminals);

        setInterval(() => {
            const badges = document.querySelectorAll('.status-badge');
            badges.forEach(badge => {
                if(badge.id && badge.id.startsWith('status_')) {
                    const ip = badge.id.replace('status_', '').replace(/_/g, '.');
                    checkStatus(ip);
                }
            });
        }, 5000);
    </script>
</body>
</html>
"""

# ==============================================================================
# ROTAS DA APLICAÇÃO WEB
# ==============================================================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/mosaics', methods=['GET'])
def api_get_mosaics():
    """
    Função de Busca Recursiva de Portas.
    Vasculha os ficheiros de configuração na pasta 'data' (exceto o dos SSH).
    """
    possible_files = ["backup_settings.json", "config_linhas.json", "linhas.json", "config.json", "mosaicos.json", "config_geral.json", "data.json"]
    all_ports = []
    
    for pf in possible_files:
        path = os.path.join(DATA_DIR, pf)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    ports = find_ports_in_json(data)
                    if ports:
                        all_ports.extend(ports)
            except Exception as e:
                logging.error(f"Erro ao analisar {pf}: {e}")
                
    if all_ports:
        unique_ports = {p['port']: p for p in all_ports}.values()
        return jsonify({"status": "success", "mosaics": list(unique_ports)})
        
    return jsonify({"status": "error", "message": "Nenhum JSON com portas encontrado."})

@app.route('/api/terminals', methods=['GET'])
def api_get_terminals():
    terminals = load_terminals()
    safe_terminals = {}
    for ip, data in terminals.items():
        safe_terminals[ip] = {"name": data.get("name", "Terminal Desconhecido"), "username": data.get("username", "pi")}
    return jsonify({"status": "success", "terminals": safe_terminals, "paramiko": PARAMIKO_AVAILABLE})

@app.route('/api/terminals/scan', methods=['GET'])
def api_scan_network():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '192.168.1.1'
        
    base_ip = local_ip.rsplit('.', 1)[0]
    found_ips = []
    
    def check_port(ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            if sock.connect_ex((ip, 22)) == 0: return ip
        except Exception: pass
        finally: sock.close()
        return None

    ips_to_check = [f"{base_ip}.{i}" for i in range(1, 255)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(check_port, ips_to_check))
        
    for res in results:
        if res and res != local_ip: found_ips.append(res)
            
    return jsonify({"status": "success", "ips": found_ips})

@app.route('/api/terminals/add', methods=['POST'])
def api_add_terminal():
    req_data = request.get_json()
    ip = req_data.get('ip', '').strip()
    name = req_data.get('name', '').strip()
    username = req_data.get('username', '').strip()
    password = req_data.get('password', '')

    if not ip or not name or not username:
        return jsonify({"status": "error", "message": "Por favor, preencha todos os campos obrigatórios."})

    terminals = load_terminals()
    terminals[ip] = {"name": name, "username": username, "password": encode_pwd(password)}
    
    if save_terminals(terminals):
        logging.info(f"Terminal adicionado/atualizado: {ip} ({name})")
        return jsonify({"status": "success", "message": "Terminal guardado com sucesso."})
    return jsonify({"status": "error", "message": "Falha ao gravar os dados no JSON."})

@app.route('/api/terminals/delete', methods=['POST'])
def api_delete_terminal():
    req_data = request.get_json()
    ip = req_data.get('ip', '').strip()
    
    terminals = load_terminals()
    if ip in terminals:
        del terminals[ip]
        save_terminals(terminals)
        logging.info(f"Terminal removido: {ip}")
        return jsonify({"status": "success", "message": "Terminal removido da base de dados."})
    return jsonify({"status": "error", "message": "O terminal solicitado não foi encontrado."})

@app.route('/api/terminals/ping', methods=['POST'])
def api_ping_terminal():
    req_data = request.get_json()
    ip = req_data.get('ip', '').strip()
    if not ip: return jsonify({"status": "error", "message": "Endereço IP inválido."})
        
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        if sock.connect_ex((ip, 22)) == 0: return jsonify({"status": "online", "ip": ip})
        else: return jsonify({"status": "offline", "ip": ip})
    except Exception: return jsonify({"status": "offline", "ip": ip})
    finally: sock.close()

@app.route('/api/terminals/execute', methods=['POST'])
def api_execute_command():
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko não instalada."})
    req_data = request.get_json()
    ip = req_data.get('ip', '').strip()
    command = req_data.get('command', '').strip()
    
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não registado."})
        
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)
        
        stdin, stdout, stderr = client.exec_command(command, timeout=10.0)
        output = stdout.read().decode('utf-8', errors='replace')
        error_output = stderr.read().decode('utf-8', errors='replace')
        client.close()
        
        final_output = output
        if error_output: final_output += f"\n[ERRO]:\n{error_output}"
        return jsonify({"status": "success", "output": final_output})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/broadcast', methods=['POST'])
def api_broadcast():
    """ Envia um comando de Reboot ou Shutdown para todos os terminais em simultâneo via Threads """
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko não instalada."})
    req_data = request.get_json()
    action = req_data.get('action')
    terminals = load_terminals()
    
    if action == 'reboot': cmd = "sudo reboot"
    elif action == 'shutdown': cmd = "sudo shutdown -h now"
    else: return jsonify({"status": "error", "message": "Ação global desconhecida."})

    def worker(ip, term):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=ip, port=22, username=term['username'], password=decode_pwd(term['password']), timeout=4.0)
            client.exec_command(cmd, timeout=2.0)
            client.close()
        except: pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for ip, term in terminals.items():
            executor.submit(worker, ip, term)
            
    return jsonify({"status": "success"})

@app.route('/api/terminals/get_real_state', methods=['POST'])
def api_get_real_state():
    """
    Entra no terminal, extrai ativamente o URL, e obriga o Raspberry a testar a acessibilidade do próprio URL!
    Lê também o estado atual do OverlayFS (Read-Only mode).
    """
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko não instalada."})
        
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não registado."})
        
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)
        
        # Procura linhas http robustamente, test HTTP, NTP, HN e OverlayFS
        read_script = """
URL=$(grep -m 1 "^http" /boot/firmware/fullpageos.txt 2>/dev/null || grep -m 1 "^http" /boot/fullpageos.txt 2>/dev/null)
URL=$(echo "$URL" | tr -d '\r' | xargs)
HTTP_CODE="000"
if [[ "$URL" == http* ]]; then
    HTTP_CODE=$(curl -s -L -o /dev/null -w "%{http_code}" --max-time 3 "$URL" || echo "000")
fi
NTP=$(grep -m 1 "^NTP=" /etc/systemd/timesyncd.conf 2>/dev/null | cut -d= -f2 || echo "")
HN=$(hostname || echo "")
if grep -q -E "^overlay" /proc/mounts; then RO="ON"; else RO="OFF"; fi

echo "URL_RES||$URL"
echo "HTTP_RES||$HTTP_CODE"
echo "NTP_RES||$NTP"
echo "HN_RES||$HN"
echo "RO_RES||$RO"
"""
        
        stdin, stdout, stderr = client.exec_command(read_script, timeout=7.0)
        output = stdout.read().decode('utf-8').strip()
        client.close()
        
        real_url = ""
        real_http = "000"
        real_ntp = ""
        real_hn = ""
        real_ro = "OFF"
        
        for line in output.split('\n'):
            if line.startswith("URL_RES||"): real_url = line.split("||")[1].strip()
            if line.startswith("HTTP_RES||"): real_http = line.split("||")[1].strip()
            if line.startswith("NTP_RES||"): real_ntp = line.split("||")[1].strip()
            if line.startswith("HN_RES||"): real_hn = line.split("||")[1].strip()
            if line.startswith("RO_RES||"): real_ro = line.split("||")[1].strip()
            
        saved_config = terminals[ip].get('config_state', {})
        
        return jsonify({
            "status": "success", 
            "real_url": real_url,
            "real_http": real_http,
            "real_ntp": real_ntp,
            "real_hostname": real_hn,
            "real_ro": real_ro,
            "config": saved_config
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/hw_status', methods=['POST'])
def api_hw_status():
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko em falta."})
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não encontrado."})

    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)

        hw_script = """
        echo "TEMP||$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 || echo 'N/A')"
        echo "UPTIME||$(uptime -p 2>/dev/null | sed 's/up //g' || echo 'N/A')"
        echo "RAM||$(free -m | awk 'NR==2{printf \"%s MB / %s MB\", $3,$2 }' || echo 'N/A')"
        echo "DISK||$(df -h / | awk 'NR==2{printf \"%s / %s (%s livre)\", $3,$2,$4 }' || echo 'N/A')"
        echo "CPU||$(cat /proc/loadavg | awk '{print $1, $2, $3}' || echo 'N/A')"
        """
        stdin, stdout, stderr = client.exec_command(hw_script, timeout=5.0)
        output = stdout.read().decode('utf-8').strip()
        client.close()

        res = {"temp": "N/A", "uptime": "N/A", "ram": "N/A", "disk": "N/A", "cpu": "N/A"}
        for line in output.split('\n'):
            if line.startswith("TEMP||"): res["temp"] = line.split("||")[1]
            elif line.startswith("UPTIME||"): res["uptime"] = line.split("||")[1]
            elif line.startswith("RAM||"): res["ram"] = line.split("||")[1]
            elif line.startswith("DISK||"): res["disk"] = line.split("||")[1]
            elif line.startswith("CPU||"): res["cpu"] = line.split("||")[1]

        return jsonify({"status": "success", "hw": res})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/scan_wifi', methods=['POST'])
def api_scan_wifi():
    """
    Rastreia redes Wi-Fi através do NMCLI do Raspberry Pi.
    """
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko em falta."})
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não encontrado."})
        
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)
        
        # Pede ao NetworkManager para listar redes Wi-Fi visíveis, omitindo vazios e ordenando por sinal
        cmd = "sudo nmcli -t -f SSID,SIGNAL dev wifi list | awk -F: '$1 != \"\" {print $0}' | sort -t: -k2 -nr | uniq"
        stdin, stdout, stderr = client.exec_command(cmd, timeout=10.0)
        output = stdout.read().decode('utf-8').strip()
        client.close()
        
        networks = []
        for line in output.split('\n'):
            if ':' in line:
                parts = line.rsplit(':', 1)
                if len(parts) == 2:
                    ssid, signal = parts[0], parts[1]
                    if ssid and ssid != "--":
                        networks.append({"ssid": ssid, "signal": signal})
        
        # Remove duplicados garantindo o sinal mais forte (já está ordenado do bash)
        unique_nets = []
        seen = set()
        for net in networks:
            if net['ssid'] not in seen:
                seen.add(net['ssid'])
                unique_nets.append(net)
                
        return jsonify({"status": "success", "networks": unique_nets})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/apply_config', methods=['POST'])
def api_apply_config():
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko não instalada."})
        
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    cfg_type = req_data.get('type', '')
    cfg_value = req_data.get('value', '')
    
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não registado."})
        
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    
    command = ""
    
    if cfg_type == 'rot':
        rot_map = {'0': 'normal', '90': 'left', '180': 'inverted', '270': 'right'}
        x11_rot = rot_map.get(cfg_value, 'normal')
        command = f"DISPLAY=:0 xrandr --output default --rotate {x11_rot} || DISPLAY=:0 xrandr -o {x11_rot}"
        
    elif cfg_type == 'screensaver':
        command = "DISPLAY=:0 xset s noblank && DISPLAY=:0 xset s off && DISPLAY=:0 xset -dpms"
        
    elif cfg_type == 'net':
        if cfg_value == 'dhcp':
            command = "sudo nmcli con mod 'Wired connection 1' ipv4.method auto && sudo nmcli con up 'Wired connection 1'"
        elif cfg_value == 'static':
            sip = req_data.get('static_ip', '')
            sgw = req_data.get('static_gw', '')
            sdns = req_data.get('static_dns', '')
            
            clean_ip = sip.split('/')[0] if '/' in sip else sip
            if clean_ip != ip:
                if is_reachable(clean_ip):
                    return jsonify({"status": "error", "message": f"Atenção: O IP {clean_ip} já está a responder na rede."})
            
            if not is_reachable(sgw):
                return jsonify({"status": "error", "message": f"A Gateway {sgw} encontra-se inacessível."})
                
            if not is_reachable(sdns) and sdns != sgw:
                return jsonify({"status": "error", "message": f"O DNS {sdns} falhou no teste de Ping."})
            
            command = f"sudo nmcli con mod 'Wired connection 1' ipv4.addresses {sip} ipv4.gateway {sgw} ipv4.dns {sdns} ipv4.method manual && sudo nmcli con up 'Wired connection 1'"
            
        elif cfg_value == 'wifi':
            ssid = req_data.get('wifi_ssid', '').replace("'", "'\\''")
            pwd = req_data.get('wifi_pwd', '').replace("'", "'\\''")
            command = f"sudo nmcli radio wifi on && sudo nmcli dev wifi connect '{ssid}' password '{pwd}'"

    elif cfg_type == 'action':
        if cfg_value == 'f5': command = "DISPLAY=:0 xdotool key F5 || DISPLAY=:0 xdotool key ctrl+r"
        elif cfg_value == 'clearcache': command = "rm -rf /home/pi/.config/chromium/Default/Cache/* && DISPLAY=:0 xdotool key F5"
        elif cfg_value == 'hidecursor': command = "killall unclutter; unclutter -idle 0.1 -root &"
        elif cfg_value == 'zoom_in': command = "DISPLAY=:0 xdotool key ctrl+plus"
        elif cfg_value == 'zoom_out': command = "DISPLAY=:0 xdotool key ctrl+minus"
        elif cfg_value == 'zoom_reset': command = "DISPLAY=:0 xdotool key ctrl+0"

    elif cfg_type == 'url':
        safe_url = cfg_value.replace("'", "'\\''")
        command = f"echo '{safe_url}' | sudo tee /boot/fullpageos.txt > /dev/null; echo '{safe_url}' | sudo tee /boot/firmware/fullpageos.txt > /dev/null 2>&1; sudo systemctl restart lightdm"
        
    elif cfg_type == 'autoreconnect':
        bash_script = """mkdir -p /home/pi/scripts
cat << 'EOF' > /home/pi/scripts/auto_reconnect.sh
#!/bin/bash
URL=\\$(grep -m 1 "^http" /boot/firmware/fullpageos.txt 2>/dev/null || grep -m 1 "^http" /boot/fullpageos.txt 2>/dev/null | tr -d '\\r')
[ -z "\\$URL" ] && URL="8.8.8.8"
WAS_DOWN=0
while true; do
    if curl -s --max-time 3 "\\$URL" > /dev/null 2>&1; then
        if [ \\$WAS_DOWN -eq 1 ]; then
            sleep 3
            DISPLAY=:0 xdotool key F5
            WAS_DOWN=0
        fi
    else
        WAS_DOWN=1
    fi
    sleep 5
done
EOF
chmod +x /home/pi/scripts/auto_reconnect.sh
pkill -f auto_reconnect.sh
nohup /home/pi/scripts/auto_reconnect.sh > /dev/null 2>&1 &
if ! grep -q "auto_reconnect.sh" /home/pi/.config/lxsession/LXDE-pi/autostart 2>/dev/null; then
    mkdir -p /home/pi/.config/lxsession/LXDE-pi/
    echo "@bash /home/pi/scripts/auto_reconnect.sh" >> /home/pi/.config/lxsession/LXDE-pi/autostart
fi
"""
        command = bash_script
        
    elif cfg_type == 'time':
        safe_time = cfg_value.replace('"', '').replace(';', '').replace('&', '')
        command = f'sudo date -s "{safe_time}"'
        
    elif cfg_type == 'ntp':
        safe_ntp = cfg_value.replace('"', '').replace(';', '').replace('&', '').strip()
        command = f"sudo sed -i '/^#*NTP=/d' /etc/systemd/timesyncd.conf && sudo sh -c 'echo \"NTP={safe_ntp}\" >> /etc/systemd/timesyncd.conf' && sudo systemctl restart systemd-timesyncd"
        
    elif cfg_type == 'hostname':
        safe_name = cfg_value.replace('"', '').replace(';', '').replace('&', '').strip()
        command = f'sudo hostnamectl set-hostname {safe_name} && sudo sed -i "s/127.0.1.1.*/127.0.1.1\\t{safe_name}/g" /etc/hosts'
        
    elif cfg_type == 'readonly':
        if cfg_value == 'on':
            command = "sudo raspi-config nonint enable_overlayfs && sudo raspi-config nonint enable_bootro && sudo reboot"
        elif cfg_value == 'off':
            command = "sudo raspi-config nonint disable_overlayfs && sudo raspi-config nonint disable_bootro && sudo reboot"

    if not command: return jsonify({"status": "error", "message": "Ação desconhecida."})

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)
        
        if cfg_type == 'net' and cfg_value in ['static', 'wifi']:
            client.exec_command(command, timeout=1.0)
        else:
            client.exec_command(command, timeout=8.0)
            
        client.close()
        
        if cfg_type not in ['action', 'url', 'time', 'hostname', 'readonly']:
            if 'config_state' not in terminals[ip]: terminals[ip]['config_state'] = {}
            if cfg_type == 'rot': terminals[ip]['config_state']['rotation'] = cfg_value
            elif cfg_type == 'screensaver': terminals[ip]['config_state']['screensaver'] = cfg_value
            elif cfg_type == 'net': terminals[ip]['config_state']['network'] = cfg_value
            elif cfg_type == 'autoreconnect': terminals[ip]['config_state']['auto_reconnect'] = cfg_value
            elif cfg_type == 'ntp': terminals[ip]['config_state']['ntp'] = cfg_value
            save_terminals(terminals)
        
        return jsonify({"status": "success"})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/screenshot', methods=['POST'])
def api_screenshot():
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error", "message": "Biblioteca Paramiko em falta."})
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error", "message": "Terminal não encontrado."})
        
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=5.0)
        
        cmd = "DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority scrot /tmp/screen.png"
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        
        if exit_status != 0:
            error_msg = stderr.read().decode('utf-8').strip()
            client.close()
            if "command not found" in error_msg or "not found" in error_msg:
                return jsonify({"status": "error", "message": "Falta o utilitário 'scrot' no Raspberry. Use a consola em baixo e envie o comando: sudo apt-get install -y scrot"})
            return jsonify({"status": "error", "message": f"O ecrã bloqueou a captura: {error_msg}"})
        
        sftp = client.open_sftp()
        try:
            with sftp.file('/tmp/screen.png', 'rb') as f:
                img_data = f.read()
        except IOError:
            sftp.close()
            client.close()
            return jsonify({"status": "error", "message": "A foto não foi guardada. O ecrã pode estar em standby ou a máquina não suporta X11."})
            
        b64_img = base64.b64encode(img_data).decode('utf-8')
        try: sftp.remove('/tmp/screen.png')
        except: pass
        sftp.close()
        client.close()
        return jsonify({"status": "success", "image": b64_img})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/terminals/uptime', methods=['POST'])
def api_get_uptime():
    if not PARAMIKO_AVAILABLE: return jsonify({"status": "error"})
    req_data = request.get_json()
    ip = req_data.get('ip', '')
    terminals = load_terminals()
    if ip not in terminals: return jsonify({"status": "error"})
    username = terminals[ip].get('username')
    password = decode_pwd(terminals[ip].get('password', ''))
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, port=22, username=username, password=password, timeout=3.0)
        stdin, stdout, stderr = client.exec_command('uptime -p', timeout=3.0)
        output = stdout.read().decode('utf-8').strip()
        client.close()
        if output.startswith("up "): output = output[3:]
        return jsonify({"status": "success", "uptime": output})
    except Exception: return jsonify({"status": "error"})

# ==============================================================================
# PONTO DE ENTRADA DO SCRIPT
# ==============================================================================
if __name__ == '__main__':
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'

    logging.info("=========================================================")
    logging.info("🤖 SISTEMA DE GESTÃO DE TERMINAIS SSH INICIADO")
    if not PARAMIKO_AVAILABLE:
        logging.info("⚠️  AVISO: 'paramiko' está em falta. Funcionalidade SSH desativada.")
    logging.info(f"🌐 Aceda pelo seu navegador: http://{local_ip}:5583")
    logging.info("=========================================================")
    
    app.run(host='0.0.0.0', port=5583, debug=False, threaded=True)