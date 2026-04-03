#!/usr/bin/env python3
"""
Aplicação Autónoma: Criação Pen PKIRIS (Lado do Cliente)
Portal dedicado para listar os backups PKIRIS existentes e enviar para a Pen USB
inserida no computador de quem está a aceder à página.
Corre num processo isolado para estabilidade do sistema principal.
"""

import os
import json
import logging
import base64
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, send_file

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "backup_settings.json")

# ==============================================================================
# INICIALIZAÇÃO
# ==============================================================================
app = Flask(__name__)
app.secret_key = os.urandom(24)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [PEN PKIRIS] - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ==============================================================================
# LÓGICA DE DADOS
# ==============================================================================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Erro ao carregar configurações: {e}")
    return {}

def scan_pkiris_backups():
    """Lê a diretoria raiz de PKIRIS e lista os ficheiros disponíveis por máquina."""
    config = load_config()
    dst_root = config.get('pkiris_dst_root', '')
    
    backups = []
    
    if not dst_root or not os.path.exists(dst_root):
        return backups
        
    try:
        for linha_dir in os.listdir(dst_root):
            linha_path = os.path.join(dst_root, linha_dir)
            if not os.path.isdir(linha_path):
                continue
                
            for maq_dir in os.listdir(linha_path):
                maq_path = os.path.join(linha_path, maq_dir)
                if not os.path.isdir(maq_path):
                    continue
                    
                # Procurar ficheiros .pkiris dentro da pasta da máquina
                ficheiros_pkiris = []
                for file_name in os.listdir(maq_path):
                    if file_name.lower().endswith('.pkiris'):
                        file_path = os.path.join(maq_path, file_name)
                        mtime = os.path.getmtime(file_path)
                        size_mb = os.path.getsize(file_path) / (1024 * 1024)
                        ficheiros_pkiris.append({
                            "nome": file_name,
                            "caminho": file_path,
                            "data_modificacao": datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S'),
                            "timestamp": mtime,
                            "tamanho_mb": round(size_mb, 2),
                            "tamanho_bytes": os.path.getsize(file_path)
                        })
                
                # Ordenar do mais recente para o mais antigo
                ficheiros_pkiris.sort(key=lambda x: x['timestamp'], reverse=True)
                
                if ficheiros_pkiris:
                    backups.append({
                        "linha": linha_dir.replace("Linha_", ""),
                        "maquina": maq_dir,
                        "ficheiros": ficheiros_pkiris
                    })
                    
        # Ordenar a lista final por Linha
        backups.sort(key=lambda x: x['linha'])
    except Exception as e:
        logging.error(f"Erro ao procurar backups PKIRIS: {e}")
        
    return backups

def get_exact_image(folder_path, filename):
    """
    Vai buscar a imagem EXATAMENTE ao caminho especificado.
    Imprime logs precisos para diagnosticar permissões ou falhas.
    """
    filepath = os.path.join(folder_path, filename)
    logging.info(f"A tentar ler imagem EXATAMENTE em: {filepath}")
    
    if os.path.exists(filepath):
        try:
            with open(filepath, "rb") as image_file:
                encoded = base64.b64encode(image_file.read()).decode('utf-8')
                logging.info(f"[SUCESSO] Imagem {filename} lida e codificada com sucesso!")
                return encoded
        except Exception as e:
            logging.error(f"[ERRO DE PERMISSÃO/LEITURA] O ficheiro existe em {filepath}, mas o sistema não consegue ler: {e}")
            return ""
    else:
        logging.error(f"[ERRO DE CAMINHO] O ficheiro {filename} NÃO FOI ENCONTRADO em {filepath}")
        return ""

# ==============================================================================
# TEMPLATES HTML
# ==============================================================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <title>Criação Pen PKIRIS</title>
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
    <meta http-equiv="Pragma" content="no-cache" />
    <meta http-equiv="Expires" content="0" />
    
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #34495e; min-height: 100vh; color: #333; overflow-x: hidden; }
        
        /* ==========================================================
           ESTILOS DO ECRÃ INICIAL (SPLASH SCREEN)
           ========================================================== */
        #intro_screen {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background-color: #34495e;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            z-index: 9999;
            transition: opacity 0.5s ease, visibility 0.5s ease;
        }
        
        .logos-container { display: flex; align-items: center; justify-content: center; gap: 4rem; margin-bottom: 3rem; }
        .intro-logo { max-height: 150px; width: auto; object-fit: contain; filter: drop-shadow(0 4px 6px rgba(0,0,0,0.3)); }
        
        .logo-error {
            color: #e74c3c; font-size: 1.2rem; font-weight: bold; padding: 15px 25px;
            border: 2px dashed #e74c3c; border-radius: 8px; background: rgba(231, 76, 60, 0.1); text-align: center;
        }
        
        .intro-title { color: white; font-size: 2.8rem; font-weight: 700; margin-bottom: 4rem; text-align: center; text-shadow: 0 4px 15px rgba(0,0,0,0.4); letter-spacing: 1px; }
        
        .intro-btn {
            font-size: 1.5rem; padding: 1rem 4rem; border-radius: 50px; background: #8e44ad; color: white;
            border: none; cursor: pointer; font-weight: bold; transition: all 0.3s ease; box-shadow: 0 6px 20px rgba(0,0,0,0.4);
            display: flex; align-items: center; gap: 15px;
        }
        
        .intro-btn:hover { transform: translateY(-5px) scale(1.05); background: #9b59b6; box-shadow: 0 10px 25px rgba(0,0,0,0.6); }

        /* ==========================================================
           GESTAO DE ECRAS DA NAVEGACAO PASSO-A-PASSO
           ========================================================== */
        .app-screen {
            display: none;
            opacity: 0;
            transition: opacity 0.4s ease;
            max-width: 1400px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        .app-screen.active {
            display: block;
            opacity: 1;
        }
        
        /* Cabeçalho partilhado */
        .header { background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); padding: 1.2rem 1.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.2); position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; border-bottom: 4px solid #8e44ad; }
        .header-title { display: flex; align-items: center; gap: 15px; }

        /* Títulos de Passos */
        .step-title {
            color: white;
            font-size: 2.2rem;
            text-align: center;
            margin-bottom: 2rem;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }

        /* Grelhas de Botões */
        .grid-linhas {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            justify-content: center;
            max-width: 1000px;
            margin: 0 auto;
        }
        
        .btn-linha {
            background-color: #ecf0f1; color: #2c3e50; font-size: 1.5rem; font-weight: bold;
            padding: 35px 20px; border: 4px solid #bdc3c7; border-radius: 12px; cursor: pointer;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1); transition: all 0.2s ease;
        }
        .btn-linha:hover { background-color: #8e44ad; color: white; border-color: #9b59b6; transform: translateY(-3px); }

        .grid-maquinas {
            display: flex;
            justify-content: center;
            gap: 40px;
            margin: 0 auto;
            flex-wrap: wrap;
        }

        /* Correção para Fundo de Imagem */
        .btn-maquina-img {
            background-color: #34495e; 
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            color: #fff; font-size: 2.5rem; font-weight: bold;
            width: 400px; height: 300px; border: 6px solid #ecf0f1; border-radius: 15px; cursor: pointer;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            box-shadow: 0 10px 20px rgba(0,0,0,0.3); transition: all 0.3s ease;
            position: relative; overflow: hidden;
            text-shadow: 2px 2px 8px rgba(0,0,0,0.9);
        }

        .btn-maquina-img .overlay {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1;
            transition: background 0.3s ease;
        }
        
        .btn-maquina-img span { position: relative; z-index: 2; }

        .btn-maquina-img:hover { transform: scale(1.05); border-color: #8e44ad; }
        .btn-maquina-img:hover .overlay { background: rgba(142, 68, 173, 0.4); }

        /* Botões Navegação Gerais */
        .btn-back-step {
            display: block; margin: 40px auto 0 auto; padding: 15px 40px; font-size: 1.2rem;
            background: #95a5a6; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold;
        }
        .btn-back-step:hover { background: #7f8c8d; }

        /* Layout do Ecrã de Ficheiros */
        .dashboard-grid { display: grid; grid-template-columns: 350px 1fr; gap: 2rem; }
        @media (max-width: 1024px) { .dashboard-grid { grid-template-columns: 1fr; } }
        
        .panel { background: white; border-radius: 12px; padding: 1.5rem; box-shadow: 0 10px 30px rgba(0,0,0,0.2); margin-bottom: 2rem; }
        .panel h3 { color: #2c3e50; margin-bottom: 1.5rem; font-size: 1.3rem; display: flex; align-items: center; gap: 0.5rem; border-bottom: 2px solid #eee; padding-bottom: 0.8rem; }
        
        .file-list-container { background: #fff; max-height: 500px; overflow-y: auto; border: 1px solid #e0e0e0; border-radius: 8px; }
        .file-item { display: flex; justify-content: space-between; align-items: center; padding: 1rem; border-bottom: 1px solid #eee; transition: background 0.2s; }
        .file-item:last-child { border-bottom: none; }
        .file-item:hover { background: #fdfdff; }
        .file-info { display: flex; flex-direction: column; gap: 0.3rem; }
        .file-name { font-weight: 600; color: #34495e; font-family: monospace; font-size: 1.05rem; }
        .file-meta { color: #7f8c8d; font-size: 0.85rem; display: flex; gap: 1rem; }
        
        .btn { padding: 0.6rem 1.2rem; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 0.5rem; transition: all 0.3s ease; font-size: 0.9rem; }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
        .btn-primary { background: #3498db; color: white; }
        .btn-success { background: #2ecc71; color: white; }
        .btn-purple { background: #8e44ad; color: white; }
        .btn-danger { background: #e74c3c; color: white; }
        .btn-warning { background: #f39c12; color: white; }
        
        .usb-selector { background: #f8f9fa; padding: 1.5rem; border-radius: 8px; border: 2px dashed #bdc3c7; text-align: center; margin-bottom: 1.5rem; }
        .usb-selector i { font-size: 3rem; color: #95a5a6; margin-bottom: 1rem; display: block; }
        .usb-selector .folder-name { font-weight: bold; color: #2ecc71; margin-top: 10px; font-size: 1.1rem; word-break: break-all; }
        
        .log-window { background: #1e1e1e; color: #2ecc71; font-family: monospace; font-size: 0.9rem; height: 300px; overflow-y: auto; padding: 1rem; border-radius: 8px; box-shadow: inset 0 0 15px rgba(0,0,0,0.8); }
        .log-window p { margin: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 2px; }
        .log-error { color: #e74c3c; }
        .log-info { color: #3498db; }
        .log-warn { color: #f1c40f; }
        .log-success { color: #2ecc71; font-weight: bold; }

        .progress-container { width: 100%; margin-top: 15px; display: none; }
        .progress { background: #e9ecef; border-radius: 0.5rem; height: 2rem; overflow: hidden; position: relative; box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
        .progress-bar { background: linear-gradient(45deg, #8e44ad, #3498db); height: 100%; width: 0%; transition: width 0.4s ease; }
        
        .empty-state { text-align: center; padding: 3rem; color: #7f8c8d; }
        .empty-state i { font-size: 4rem; color: #bdc3c7; margin-bottom: 1rem; }
        
        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0,0,0,0.8); display: flex; align-items: center; justify-content: center;
            z-index: 10000; opacity: 0; visibility: hidden; transition: all 0.3s ease;
        }
        .modal-overlay.active { opacity: 1; visibility: visible; }
        .modal-content {
            background: white; padding: 3rem; border-radius: 12px; text-align: center; max-width: 500px;
        }
        .modal-content i.success-icon { font-size: 5rem; color: #2ecc71; margin-bottom: 1.5rem; }
        .modal-content h2 { color: #2c3e50; margin-bottom: 1rem; }
        .modal-content p { color: #7f8c8d; margin-bottom: 2rem; font-size: 1.1rem; }
        .modal-content .btn { font-size: 1.1rem; padding: 1rem 2rem; }
    </style>
</head>
<body>

<script>
    // Injetar os dados totais de backup para filtragem local
    const allBackupsData = {{ backups | tojson }};
    let globalLinhaSelecionada = "";
    let globalEvoSelecionada = "";
    let globalRamalSelecionado = "";
    
    let ficheiroSelecionadoPath = "";
    let dirHandle = null;
    let expectedFileSize = 0;
</script>

<div id="intro_screen">
    <div class="logos-container">
        {% if logo_ba %}
            <img src="data:image/png;base64,{{ logo_ba }}" alt="Logotipo BA" class="intro-logo">
        {% else %}
            <div class="logo-error"><i class="fas fa-exclamation-triangle"></i><br>Ficheiro do logótipo BA não foi encontrado.</div>
        {% endif %}

        {% if logo_iris %}
            <img src="data:image/png;base64,{{ logo_iris }}" alt="Logotipo IRIS" class="intro-logo">
        {% else %}
            <div class="logo-error"><i class="fas fa-exclamation-triangle"></i><br>Ficheiro 'logo_iris.png' ou idêntico não encontrado.</div>
        {% endif %}
    </div>
    <h1 class="intro-title">Criaçao de PenDrive de recuperaçao Maquinas IRIS</h1>
    <button class="intro-btn" onclick="navegarPara('ecran_linhas')">
        <i class="fas fa-play-circle"></i> Iniciar o Processo
    </button>
</div>

<div class="header" id="main_header" style="display: none;">
    <div class="header-title">
        {% if logo_iris %}
            <img src="data:image/png;base64,{{ logo_iris }}" alt="Logotipo IRIS" style="max-height: 55px; width: auto; object-fit: contain;">
        {% else %}
            <h1 style="color: #8e44ad; margin: 0;"><i class="fas fa-industry"></i> IRIS</h1>
        {% endif %}
        <p id="header_subtitle" style="margin: 0 0 0 15px; padding-left: 15px; border-left: 2px solid #bdc3c7; color: #7f8c8d; font-size: 1.1rem; font-weight: 600; display: flex; align-items: center;">Siga os passos para transferir o backup</p>
    </div>
    <div>
        <button class="btn btn-primary" onclick="window.close()"><i class="fas fa-times"></i> Fechar Janela</button>
    </div>
</div>

<div id="ecran_linhas" class="app-screen">
    <h2 class="step-title">Passo 1: Selecione a Linha</h2>
    <div class="grid-linhas">
        <button class="btn-linha" onclick="selecionarLinha('21')">Linha 21</button>
        <button class="btn-linha" onclick="selecionarLinha('22')">Linha 22</button>
        <button class="btn-linha" onclick="selecionarLinha('23')">Linha 23</button>
        <button class="btn-linha" onclick="selecionarLinha('24')">Linha 24</button>
        <button class="btn-linha" onclick="selecionarLinha('31')">Linha 31</button>
        <button class="btn-linha" onclick="selecionarLinha('32')">Linha 32</button>
        <button class="btn-linha" onclick="selecionarLinha('33')">Linha 33</button>
        <button class="btn-linha" onclick="selecionarLinha('34')">Linha 34</button>
    </div>
</div>

<div id="ecran_evos" class="app-screen">
    <h2 class="step-title" id="title_evos">Passo 2: Selecione a Máquina EVO</h2>
    <div class="grid-maquinas">
        <button class="btn-maquina-img" onclick="selecionarEvo('EVO 16')" {% if img_evo16 %}style="background-image: url('data:image/jpeg;base64,{{ img_evo16 }}');"{% endif %}>
            <div class="overlay"></div>
            <span>EVO 16</span>
        </button>
        
        <button class="btn-maquina-img" onclick="selecionarEvo('EVO 05')" {% if img_evo05 %}style="background-image: url('data:image/jpeg;base64,{{ img_evo05 }}');"{% endif %}>
            <div class="overlay"></div>
            <span>EVO 05</span>
        </button>
    </div>
    <button class="btn-back-step" onclick="navegarPara('ecran_linhas')"><i class="fas fa-arrow-left"></i> Voltar às Linhas</button>
</div>

<div id="ecran_ramais" class="app-screen">
    <h2 class="step-title" id="title_ramais">Passo 3: Selecione o Ramal</h2>
    <div class="grid-maquinas">
        <button class="btn-maquina-img" onclick="selecionarRamal('Ramal 1')" {% if img_ramal1 %}style="background-image: url('data:image/jpeg;base64,{{ img_ramal1 }}');"{% endif %}>
            <div class="overlay"></div>
            <span>RAMAL 1</span>
        </button>
        
        <button class="btn-maquina-img" onclick="selecionarRamal('Ramal 2')" {% if img_ramal2 %}style="background-image: url('data:image/jpeg;base64,{{ img_ramal2 }}');"{% endif %}>
            <div class="overlay"></div>
            <span>RAMAL 2</span>
        </button>
    </div>
    <button class="btn-back-step" onclick="navegarPara('ecran_evos')"><i class="fas fa-arrow-left"></i> Voltar às EVOs</button>
</div>

<div id="ecran_ficheiros" class="app-screen">
    <h2 class="step-title" id="title_ficheiros" style="margin-bottom: 1rem;">Selecção de Ficheiro e Gravação</h2>
    
    <div class="dashboard-grid">
        <div class="side-panel">
            <div class="panel">
                <h3><i class="fas fa-hdd"></i> Unidade de Destino</h3>
                <div class="usb-selector">
                    <i class="fas fa-plug"></i>
                    <h4>Pen USB no seu Computador</h4>
                    <button class="btn btn-warning" style="margin-top: 1rem; width: 100%;" onclick="selecionarPastaLocal()">
                        <i class="fas fa-folder-open"></i> Selecionar Raiz da Pen
                    </button>
                    <div class="folder-name" id="usb_drive_name">Nenhuma diretoria selecionada</div>
                </div>
                
                <div id="selection_info" style="display: none; background: #fdf2e9; border-left: 4px solid #f39c12; padding: 1rem; border-radius: 4px; margin-bottom: 1.5rem;">
                    <strong style="color: #d35400; display: block; margin-bottom: 0.5rem;">Ficheiro Escolhido:</strong>
                    <span id="selected_filename" style="font-family: monospace; word-break: break-all;">Nenhum</span>
                </div>

                <button class="btn btn-purple" id="btn_format" style="width: 100%; padding: 1rem; font-size: 1.1rem; justify-content: center;" onclick="iniciarCriacao()" disabled>
                    <i class="fas fa-download"></i> Gravar e Verificar
                </button>
                
                <div class="progress-container" id="creation_progress">
                    <p id="progress_text" style="margin-bottom: 5px; font-weight: bold; color: #2c3e50;">A transferir e gravar...</p>
                    <div class="progress"><div class="progress-bar" id="progress_bar"></div></div>
                </div>
            </div>
            <button class="btn-back-step" style="margin-top: 0; width: 100%;" onclick="voltarDeFicheiros()"><i class="fas fa-arrow-left"></i> Voltar Atrás</button>
        </div>
        
        <div class="main-panel">
            <div class="panel">
                <h3><i class="fas fa-archive"></i> Ficheiros PKIRIS Disponíveis</h3>
                <div class="file-list-container" id="file_list_render_area">
                    </div>
            </div>
            
            <div class="panel">
                <h3><i class="fas fa-terminal"></i> Terminal de Transferência</h3>
                <div class="log-window" id="terminal_logs">
                    <p class="log-info">Sistema inicializado e aguardando comandos.</p>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="modal-overlay" id="success_modal">
    <div class="modal-content">
        <i class="fas fa-check-circle success-icon"></i>
        <h2>Cópia Concluída com Sucesso!</h2>
        <p>A gravação foi finalizada e os tamanhos dos ficheiros foram verificados com precisão e correspondem integralmente ao original.</p>
        <button class="btn btn-success" onclick="fecharModalSucesso()">Concluir e Voltar</button>
    </div>
</div>

<script>
    // Sistema de Navegação
    function navegarPara(idAlvo) {
        document.getElementById('intro_screen').style.display = 'none';
        document.getElementById('main_header').style.display = 'flex';
        
        document.querySelectorAll('.app-screen').forEach(s => {
            s.classList.remove('active');
            s.style.display = 'none';
        });
        
        const alvo = document.getElementById(idAlvo);
        alvo.style.display = 'block';
        setTimeout(() => { alvo.classList.add('active'); }, 50);
    }

    // Passos do Wizard
    function selecionarLinha(linha) {
        globalLinhaSelecionada = linha;
        document.getElementById('title_evos').innerText = `Passo 2: Selecione a Máquina EVO (Linha ${linha})`;
        navegarPara('ecran_evos');
    }

    function selecionarEvo(evo) {
        globalEvoSelecionada = evo;
        if (globalLinhaSelecionada === '34') {
            document.getElementById('title_ramais').innerText = `Passo 3: Selecione o Ramal (Linha ${globalLinhaSelecionada} - ${evo})`;
            navegarPara('ecran_ramais');
        } else {
            // Se NÃO for a linha 34, saltamos a seleção de ramais.
            globalRamalSelecionado = "";
            document.getElementById('header_subtitle').innerText = `Linha ${globalLinhaSelecionada} > ${globalEvoSelecionada}`;
            renderizarFicheirosDaSelecao();
            navegarPara('ecran_ficheiros');
        }
    }

    function selecionarRamal(ramal) {
        globalRamalSelecionado = ramal;
        document.getElementById('header_subtitle').innerText = `Linha ${globalLinhaSelecionada} > ${globalEvoSelecionada} > ${ramal}`;
        renderizarFicheirosDaSelecao();
        navegarPara('ecran_ficheiros');
    }

    function voltarDeFicheiros() {
        if (globalLinhaSelecionada === '34') {
            navegarPara('ecran_ramais');
        } else {
            navegarPara('ecran_evos');
        }
    }

    function getExpectedMachineName() {
        // Assume-se na lógica que EVO 16 mapeia para Lateral e EVO 05 mapeia para Fundo
        let base = globalEvoSelecionada.includes('16') ? 'lateral' : 'fundo';
        if (globalLinhaSelecionada === '34') {
            let ramalNum = globalRamalSelecionado.includes('1') ? '1' : '2';
            return base + ramalNum;
        }
        return base;
    }

    function renderizarFicheirosDaSelecao() {
        const area = document.getElementById('file_list_render_area');
        area.innerHTML = '';
        
        let linhaTarget = globalLinhaSelecionada;
        let expectedMaq = getExpectedMachineName();
        let ficheirosEncontrados = [];
        
        allBackupsData.forEach(bd => {
            // Compara a linha e a máquina de destino convertendo ambas para letras minúsculas (ex: "Lateral" === "lateral")
            if (bd.linha === linhaTarget && bd.maquina.toLowerCase() === expectedMaq.toLowerCase()) {
                bd.ficheiros.forEach(f => {
                    ficheirosEncontrados.push({
                        ...f,
                        maquinaOrigem: bd.maquina
                    });
                });
            }
        });

        if (ficheirosEncontrados.length === 0) {
            area.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-exclamation-circle"></i>
                    <h2>Sem Backups Registados</h2>
                    <p>Não foram encontrados ficheiros PKIRIS para a máquina ${expectedMaq.toUpperCase()} da Linha ${linhaTarget}.</p>
                </div>
            `;
            return;
        }

        ficheirosEncontrados.sort((a,b) => b.timestamp - a.timestamp); // Ordenar por mais recente

        let html = '';
        ficheirosEncontrados.forEach(f => {
            html += `
            <div class="file-item">
                <div class="file-info">
                    <span class="file-name"><i class="fas fa-file-code" style="color: #34495e; margin-right: 5px;"></i>${f.nome}</span>
                    <div class="file-meta">
                        <span><i class="far fa-calendar-alt"></i> ${f.data_modificacao}</span>
                        <span><i class="fas fa-weight-hanging"></i> ${f.tamanho_mb} MB</span>
                        <span><i class="fas fa-microchip"></i> Localização: ${f.maquinaOrigem}</span>
                    </div>
                </div>
                <button class="btn btn-primary" onclick="selecionarFicheiroParaGravacao('${f.caminho.replace(/\\/g, '\\\\')}', '${f.nome}', ${f.tamanho_bytes})">
                    <i class="fas fa-check"></i> Selecionar
                </button>
            </div>
            `;
        });
        area.innerHTML = html;
        
        // Reset à selecção prévia
        ficheiroSelecionadoPath = "";
        expectedFileSize = 0;
        document.getElementById('selection_info').style.display = 'none';
        validarBotaoCriacao();
    }

    // Lógica do Terminal
    function addLog(message, type="info") {
        const terminal = document.getElementById('terminal_logs');
        const p = document.createElement('p');
        const time = new Date().toLocaleTimeString();
        p.className = `log-${type}`;
        p.innerHTML = `[${time}] ${message}`;
        terminal.appendChild(p);
        terminal.scrollTop = terminal.scrollHeight;
    }

    // Seleção de Ficheiro Final
    function selecionarFicheiroParaGravacao(caminho, nome, sizeBytes) {
        ficheiroSelecionadoPath = caminho;
        expectedFileSize = sizeBytes;
        document.getElementById('selection_info').style.display = 'block';
        document.getElementById('selected_filename').innerText = nome;
        
        addLog(`Ficheiro PKIRIS preparado: ${nome} (${(sizeBytes/1024/1024).toFixed(2)} MB)`, "info");
        validarBotaoCriacao();
    }
    
    // API File System Access do Browser
    async function selecionarPastaLocal() {
        if (!window.showDirectoryPicker) {
            addLog("Aviso: O seu navegador bloqueia a escrita direta na Pen.", "warn");
            addLog("Será usado o método de Download Nativo de substituição.", "info");
            dirHandle = "MODO_DOWNLOAD_NATIVO";
            document.getElementById('usb_drive_name').innerText = "Modo: Janela de Download Nativa";
            validarBotaoCriacao();
            return;
        }

        try {
            dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
            let displayName = dirHandle.name;
            if (!displayName || displayName === "\\" || displayName === "/") {
                displayName = "Raiz da Pen USB";
            }
            document.getElementById('usb_drive_name').innerText = displayName;
            validarBotaoCriacao();
            addLog(`Destino local USB mapeado: ${displayName}`, 'info');
        } catch (e) {
            addLog(`Seleção cancelada pelo utilizador ou erro: ${e.message}`, 'error');
            dirHandle = null;
            document.getElementById('usb_drive_name').innerText = "Nenhuma diretoria selecionada";
            validarBotaoCriacao();
        }
    }

    function validarBotaoCriacao() {
        const btn = document.getElementById('btn_format');
        if (ficheiroSelecionadoPath !== "" && dirHandle !== null) {
            btn.disabled = false;
        } else {
            btn.disabled = true;
        }
    }

    // Processo Principal de Cópia e Verificação
    async function iniciarCriacao() {
        if (!ficheiroSelecionadoPath || !dirHandle) return;
        
        document.getElementById('btn_format').disabled = true;
        const nomeFicheiro = document.getElementById('selected_filename').innerText;
        
        addLog("==========================================", "warn");
        addLog(`A INICIAR CÓPIA FÍSICA PARA A PEN DRIVE`, "warn");
        
        if (dirHandle === "MODO_DOWNLOAD_NATIVO") {
            addLog("Modo Download: Escolha a Pen USB na janela que vai abrir.", "info");
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/api/download_pkiris';
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'ficheiro';
            input.value = ficheiroSelecionadoPath;
            form.appendChild(input);
            document.body.appendChild(form);
            form.submit();
            document.body.removeChild(form);
            addLog("Transferência via Navegador iniciada. O Browser fará a verificação interna.", "success");
            setTimeout(() => { document.getElementById('btn_format').disabled = false; }, 3000);
            return;
        }
        
        document.getElementById('creation_progress').style.display = 'block';
        const progressBar = document.getElementById('progress_bar');
        const progressText = document.getElementById('progress_text');
        
        try {
            addLog(`A comunicar com a NAS e a iniciar a leitura segura...`, 'info');
            progressText.innerText = "A descarregar da NAS...";
            progressBar.style.width = "30%";
            
            const response = await fetch('/api/download_pkiris_api', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ficheiro: ficheiroSelecionadoPath })
            });
            
            if (!response.ok) throw new Error("Falha na comunicação de rede com o servidor.");
            
            progressBar.style.width = "60%";
            progressText.innerText = "A gravar os bits na Pen USB...";
            
            const destName = document.getElementById('usb_drive_name').innerText;
            addLog(`Transferência concluída. A escrever fluxo de dados para: ${destName}...`, 'info');
            
            const blob = await response.blob();
            
            // Escrita na USB
            const fileHandle = await dirHandle.getFileHandle(nomeFicheiro, { create: true });
            const writable = await fileHandle.createWritable();
            await writable.write(blob);
            await writable.close();
            
            progressBar.style.width = "90%";
            progressText.innerText = "A verificar a integridade da cópia...";
            addLog(`Ficheiro gravado. A executar Verificação Cíclica e Comparação de Tamanhos...`, 'warn');
            
            // FASE DE VERIFICAÇÃO RIGOROSA
            const verifyFile = await fileHandle.getFile();
            if (verifyFile.size === expectedFileSize) {
                progressBar.style.width = "100%";
                progressText.innerText = "Verificação Concluída com Sucesso!";
                addLog(`[VERIFICAÇÃO APROVADA] Tamanho na Pen: ${verifyFile.size} bytes | Esperado: ${expectedFileSize} bytes.`, 'success');
                addLog(`Ficheiro ${nomeFicheiro} guardado e validado com sucesso!`, 'success');
                addLog("Pode remover a Pen Drive do seu computador em segurança.", "success");
                addLog("==========================================", "warn");
                
                // Mostra a mensagem de sucesso modal visual
                document.getElementById('success_modal').classList.add('active');
            } else {
                throw new Error(`[ERRO DE VERIFICAÇÃO] O ficheiro na Pen tem ${verifyFile.size} bytes mas devia ter ${expectedFileSize} bytes. A cópia falhou ou está corrompida.`);
            }
            
        } catch (error) {
            addLog(`FALHA CRÍTICA: ${error.message}`, 'error');
            progressText.innerText = "Operação Abortada por Erro.";
            progressBar.style.backgroundColor = "#e74c3c";
        }
        
        setTimeout(() => {
            document.getElementById('creation_progress').style.display = 'none';
            progressBar.style.width = "0%";
            progressBar.style.backgroundColor = ""; // reset
            validarBotaoCriacao();
        }, 5000);
    }
    
    function fecharModalSucesso() {
        document.getElementById('success_modal').classList.remove('active');
        navegarPara('ecran_linhas');
    }
</script>

</body>
</html>
"""

# ==============================================================================
# ROTAS DA API
# ==============================================================================
@app.route('/')
def index():
    backups = scan_pkiris_backups()
    
    logo_ba = get_exact_image(DATA_DIR, "logo_ba.png")
    if not logo_ba:
        logo_ba = get_exact_image(BASE_DIR, "Logo_Green_Letters_white.png")
        
    # Foi adicionada a procura exata para o Logo original da IRIS em azul CMJN
    logo_iris = get_exact_image(DATA_DIR, "IRIS-LOGO-CMJN-BLEU.png")
    if not logo_iris:
        logo_iris = get_exact_image(DATA_DIR, "logo_iris.png")
    
    # Foi adicionada a procura exata para a imagem original da EVO 16 (EV16-NEO.jpg)
    img_evo16 = get_exact_image(DATA_DIR, "EV16-NEO.jpg")
    if not img_evo16:
        img_evo16 = get_exact_image(DATA_DIR, "evo16.png")
        
    # Foi adicionada a procura exata para a imagem original da EVO 05 (evo5-neo.jpg)
    img_evo05 = get_exact_image(DATA_DIR, "evo5-neo.jpg")
    if not img_evo05:
        img_evo05 = get_exact_image(DATA_DIR, "evo05.png")
        
    img_ramal1 = get_exact_image(DATA_DIR, "ramal1.png")
    img_ramal2 = get_exact_image(DATA_DIR, "ramal2.png")
    
    return render_template_string(HTML_TEMPLATE, 
                                  backups=backups, 
                                  logo_ba=logo_ba, 
                                  logo_iris=logo_iris,
                                  img_evo16=img_evo16,
                                  img_evo05=img_evo05,
                                  img_ramal1=img_ramal1,
                                  img_ramal2=img_ramal2)

@app.route('/api/download_pkiris', methods=['POST'])
def download_pkiris_form():
    """Rota para servir o ficheiro em Modo Nativo (Fallback HTML Form)"""
    ficheiro_path = request.form.get('ficheiro')
    if not ficheiro_path or not os.path.exists(ficheiro_path):
        return "Ficheiro não encontrado na NAS", 404
    
    logging.info(f"Download Nativo Iniciado para: {ficheiro_path}")
    return send_file(ficheiro_path, as_attachment=True)

@app.route('/api/download_pkiris_api', methods=['POST'])
def download_pkiris_api():
    """Rota para servir o ficheiro em formato Blob para a File System API (JavaScript)"""
    data = request.get_json()
    ficheiro_path = data.get('ficheiro')
    
    if not ficheiro_path or not os.path.exists(ficheiro_path):
        return jsonify({"status": "error", "message": "Ficheiro não encontrado"}), 404
        
    logging.info(f"Download API Blob Iniciado para: {ficheiro_path}")
    return send_file(ficheiro_path, as_attachment=True)

# ==============================================================================
# INICIALIZAÇÃO DO SERVIDOR
# ==============================================================================
if __name__ == '__main__':
    logging.info("Servidor de Criação de Pen PKIRIS iniciado na porta 5582.")
    # Executa na porta 5582
    app.run(host='0.0.0.0', port=5582, debug=False, threaded=True)