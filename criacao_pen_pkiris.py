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
                            "tamanho_mb": round(size_mb, 2)
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
        
        /* Fundo azul sólido para o painel inicial e principal */
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
        
        .logos-container {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4rem;
            margin-bottom: 3rem;
        }
        
        .intro-logo {
            max-height: 150px;
            width: auto;
            object-fit: contain;
            filter: drop-shadow(0 4px 6px rgba(0,0,0,0.3));
        }
        
        .logo-error {
            color: #e74c3c;
            font-size: 1.2rem;
            font-weight: bold;
            padding: 15px 25px;
            border: 2px dashed #e74c3c;
            border-radius: 8px;
            background: rgba(231, 76, 60, 0.1);
            text-align: center;
        }
        
        .intro-title {
            color: white;
            font-size: 2.8rem;
            font-weight: 700;
            margin-bottom: 4rem;
            text-align: center;
            text-shadow: 0 4px 15px rgba(0,0,0,0.4);
            letter-spacing: 1px;
        }
        
        .intro-btn {
            font-size: 1.5rem;
            padding: 1rem 4rem;
            border-radius: 50px;
            background: #8e44ad;
            color: white;
            border: none;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s ease;
            box-shadow: 0 6px 20px rgba(0,0,0,0.4);
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .intro-btn:hover {
            transform: translateY(-5px) scale(1.05);
            background: #9b59b6;
            box-shadow: 0 10px 25px rgba(0,0,0,0.6);
        }

        /* ==========================================================
           ESTILOS DO PAINEL PRINCIPAL
           ========================================================== */
        .header { background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); padding: 1.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.2); position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; border-bottom: 4px solid #8e44ad; }
        .header-title { display: flex; align-items: center; gap: 15px; }
        .header-title i { font-size: 2.5rem; color: #8e44ad; }
        .header h1 { color: #2c3e50; font-size: 2rem; margin-bottom: 0.2rem; }
        .header p { color: #7f8c8d; font-size: 1rem; margin: 0; }
        
        .container { max-width: 1400px; margin: 2rem auto; padding: 0 2rem; }
        
        .dashboard-grid { display: grid; grid-template-columns: 350px 1fr; gap: 2rem; }
        
        @media (max-width: 1024px) {
            .dashboard-grid { grid-template-columns: 1fr; }
        }
        
        .panel { background: white; border-radius: 12px; padding: 1.5rem; box-shadow: 0 10px 30px rgba(0,0,0,0.2); margin-bottom: 2rem; }
        .panel h3 { color: #2c3e50; margin-bottom: 1.5rem; font-size: 1.3rem; display: flex; align-items: center; gap: 0.5rem; border-bottom: 2px solid #eee; padding-bottom: 0.8rem; }
        
        /* Estilos da Lista de Backups */
        .machine-card { border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 1rem; overflow: hidden; }
        .machine-header { background: #f8f9fa; padding: 1rem; display: flex; justify-content: space-between; align-items: center; cursor: pointer; transition: background 0.2s; border-left: 4px solid #8e44ad; }
        .machine-header:hover { background: #f1f3f5; }
        .machine-title { font-weight: bold; color: #2c3e50; font-size: 1.1rem; }
        .badge { background: #8e44ad; color: white; padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.8rem; font-weight: bold; }
        
        .file-list { display: none; padding: 0; list-style: none; background: white; }
        .file-list.active { display: block; }
        .file-item { display: flex; justify-content: space-between; align-items: center; padding: 1rem; border-top: 1px solid #eee; transition: background 0.2s; }
        .file-item:hover { background: #fdfdff; }
        .file-info { display: flex; flex-direction: column; gap: 0.3rem; }
        .file-name { font-weight: 600; color: #34495e; font-family: monospace; font-size: 1.05rem; }
        .file-meta { color: #7f8c8d; font-size: 0.85rem; display: flex; gap: 1rem; }
        
        /* Botões e Formulário */
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
        
        /* Log Window */
        .log-window { background: #1e1e1e; color: #2ecc71; font-family: monospace; font-size: 0.9rem; height: 300px; overflow-y: auto; padding: 1rem; border-radius: 8px; box-shadow: inset 0 0 15px rgba(0,0,0,0.8); }
        .log-window p { margin: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 2px; }
        .log-error { color: #e74c3c; }
        .log-info { color: #3498db; }
        .log-warn { color: #f1c40f; }

        .progress-container { width: 100%; margin-top: 15px; display: none; }
        .progress { background: #e9ecef; border-radius: 0.5rem; height: 2rem; overflow: hidden; position: relative; box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
        .progress-bar { background: linear-gradient(45deg, #8e44ad, #3498db); height: 100%; width: 0%; transition: width 0.4s ease; }
        
        .empty-state { text-align: center; padding: 3rem; color: #7f8c8d; }
        .empty-state i { font-size: 4rem; color: #bdc3c7; margin-bottom: 1rem; }
    </style>
</head>
<body>

<div id="intro_screen">
    <div class="logos-container">
        {% if logo_ba %}
            <img src="data:image/png;base64,{{ logo_ba }}" alt="Logotipo BA" class="intro-logo">
        {% else %}
            <div class="logo-error">
                <i class="fas fa-exclamation-triangle"></i><br>
                Ficheiro do logótipo BA não foi encontrado.
            </div>
        {% endif %}

        {% if logo_iris %}
            <img src="data:image/png;base64,{{ logo_iris }}" alt="Logotipo IRIS" class="intro-logo">
        {% else %}
            <div class="logo-error">
                <i class="fas fa-exclamation-triangle"></i><br>
                Ficheiro 'logo_iris.png' não encontrado na pasta 'data'.<br>Verifique os logs no terminal.
            </div>
        {% endif %}
    </div>
    
    <h1 class="intro-title">Criaçao de PenDrive de recuperaçao Maquinas IRIS</h1>
    
    <button class="intro-btn" onclick="iniciarAplicacao()">
        <i class="fas fa-play-circle"></i> Iniciar o Processo
    </button>
</div>

<div id="main_app" style="display: none; opacity: 0; transition: opacity 0.5s ease;">
    <div class="header">
        <div class="header-title">
            <i class="fas fa-usb"></i>
            <div>
                <h1>Criação Pen PKIRIS</h1>
                <p>Transfira o backup diretamente para a Pen USB do seu computador</p>
            </div>
        </div>
        <div>
            <button class="btn btn-primary" onclick="window.close()"><i class="fas fa-arrow-left"></i> Voltar ao Sistema</button>
        </div>
    </div>

    <div class="container">
        <div class="dashboard-grid">
            
            <div class="side-panel">
                <div class="panel">
                    <h3><i class="fas fa-hdd"></i> Unidade de Destino Local</h3>
                    <div class="usb-selector">
                        <i class="fas fa-plug"></i>
                        <h4>Pen USB no seu Computador</h4>
                        <p style="color: #7f8c8d; font-size: 0.9rem; margin-top: 0.5rem;">Insira a Pen e clique no botão abaixo para escolher a diretoria da Pen.</p>
                        
                        <button class="btn btn-warning" style="margin-top: 1rem; width: 100%;" onclick="selecionarPastaLocal()">
                            <i class="fas fa-folder-open"></i> Selecionar Raiz da Pen
                        </button>
                        
                        <div class="folder-name" id="usb_drive_name">Nenhuma diretoria selecionada</div>
                    </div>
                    
                    <div id="selection_info" style="display: none; background: #fdf2e9; border-left: 4px solid #f39c12; padding: 1rem; border-radius: 4px; margin-bottom: 1.5rem;">
                        <strong style="color: #d35400; display: block; margin-bottom: 0.5rem;">Ficheiro Selecionado:</strong>
                        <span id="selected_filename" style="font-family: monospace; word-break: break-all;">Nenhum</span>
                    </div>

                    <button class="btn btn-purple" id="btn_format" style="width: 100%; padding: 1rem; font-size: 1.1rem; justify-content: center;" onclick="iniciarCriacao()" disabled>
                        <i class="fas fa-download"></i> Gravar na Pen USB
                    </button>
                    
                    <div class="progress-container" id="creation_progress">
                        <p id="progress_text" style="margin-bottom: 5px; font-weight: bold; color: #2c3e50;">A transferir e gravar...</p>
                        <div class="progress">
                            <div class="progress-bar" id="progress_bar"></div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="main-panel">
                <div class="panel">
                    <h3><i class="fas fa-archive"></i> Backups PKIRIS na NAS</h3>
                    
                    {% if not backups %}
                    <div class="empty-state">
                        <i class="fas fa-folder-open"></i>
                        <h2>Nenhum backup encontrado</h2>
                        <p>Verifique se a diretoria raiz de destino PKIRIS está configurada corretamente no painel principal e se as máquinas já geraram ficheiros.</p>
                    </div>
                    {% else %}
                        {% for linha_data in backups %}
                        <div class="machine-card">
                            <div class="machine-header" onclick="toggleFolder('folder_{{ loop.index }}')">
                                <div class="machine-title">
                                    <i class="fas fa-industry" style="color: #8e44ad; margin-right: 10px;"></i>
                                    Linha {{ linha_data.linha }} - {{ linha_data.maquina }}
                                </div>
                                <span class="badge">{{ linha_data.ficheiros|length }} Ficheiros</span>
                            </div>
                            <ul class="file-list" id="folder_{{ loop.index }}">
                                {% for ficheiro in linha_data.ficheiros %}
                                <li class="file-item">
                                    <div class="file-info">
                                        <span class="file-name"><i class="fas fa-file-code" style="color: #34495e; margin-right: 5px;"></i>{{ ficheiro.nome }}</span>
                                        <div class="file-meta">
                                            <span><i class="far fa-calendar-alt"></i> {{ ficheiro.data_modificacao }}</span>
                                            <span><i class="fas fa-weight-hanging"></i> {{ ficheiro.tamanho_mb }} MB</span>
                                        </div>
                                    </div>
                                    <button class="btn btn-primary btn-sm" onclick="selecionarFicheiro('{{ ficheiro.caminho|replace('\\', '\\\\') }}', '{{ ficheiro.nome }}')">
                                        <i class="fas fa-hand-pointer"></i> Selecionar
                                    </button>
                                </li>
                                {% endfor %}
                            </ul>
                        </div>
                        {% endfor %}
                    {% endif %}
                </div>
                
                <div class="panel">
                    <h3><i class="fas fa-terminal"></i> Terminal de Transferência</h3>
                    <div class="log-window" id="terminal_logs">
                        <p class="log-info">Sistema inicializado no lado do cliente.</p>
                        <p class="log-warn">Atenção: A formatação prévia da Pen USB para FAT32 deve ser feita manualmente pelo seu sistema operativo (Windows/Mac).</p>
                        <p class="log-info">Aguardando seleção de ficheiro e diretoria de destino.</p>
                    </div>
                </div>
            </div>
            
        </div>
    </div>
</div>

<script>
    let ficheiroSelecionadoPath = "";
    let dirHandle = null;

    // Transição do Ecrã Inicial para a App
    function iniciarAplicacao() {
        const intro = document.getElementById('intro_screen');
        const app = document.getElementById('main_app');
        
        intro.style.opacity = '0';
        setTimeout(() => {
            intro.style.visibility = 'hidden';
            intro.style.display = 'none';
            app.style.display = 'block';
            
            // Reflow para a transição
            void app.offsetWidth;
            app.style.opacity = '1';
        }, 500);
    }

    function toggleFolder(id) {
        const el = document.getElementById(id);
        if (el.classList.contains('active')) {
            el.classList.remove('active');
        } else {
            document.querySelectorAll('.file-list').forEach(list => list.classList.remove('active'));
            el.classList.add('active');
        }
    }

    function addLog(message, type="info") {
        const terminal = document.getElementById('terminal_logs');
        const p = document.createElement('p');
        const time = new Date().toLocaleTimeString();
        p.className = `log-${type}`;
        p.innerHTML = `[${time}] ${message}`;
        terminal.appendChild(p);
        terminal.scrollTop = terminal.scrollHeight;
    }

    function selecionarFicheiro(caminho, nome) {
        ficheiroSelecionadoPath = caminho;
        document.getElementById('selection_info').style.display = 'block';
        document.getElementById('selected_filename').innerText = nome;
        
        addLog(`Ficheiro da NAS selecionado: ${nome}`, "info");
        validarBotaoCriacao();
    }
    
    async function selecionarPastaLocal() {
        if (!window.showDirectoryPicker) {
            addLog("Aviso: O seu navegador ou ligação bloqueia a escolha direta de pastas (requer HTTPS ou Localhost).", "warn");
            addLog("Será usado o método de Download Nativo. Apenas grave o ficheiro na Pen quando aparecer a janela de sistema.", "info");
            dirHandle = "MODO_DOWNLOAD_NATIVO";
            document.getElementById('usb_drive_name').innerText = "Modo: Janela de Download Nativa";
            validarBotaoCriacao();
            return;
        }

        try {
            dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
            let displayName = dirHandle.name;
            
            // No Windows, a raiz da pen aparece como vazio ou "\" ou "/"
            if (!displayName || displayName === "\\" || displayName === "/") {
                displayName = "Raiz da Pen USB";
            }
            
            document.getElementById('usb_drive_name').innerText = displayName;
            validarBotaoCriacao();
            addLog(`Destino local selecionado: ${displayName}`, 'info');
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

    async function iniciarCriacao() {
        if (!ficheiroSelecionadoPath || !dirHandle) return;
        
        document.getElementById('btn_format').disabled = true;
        const nomeFicheiro = document.getElementById('selected_filename').innerText;
        
        addLog("==========================================", "warn");
        addLog(`INICIANDO TRANSFERÊNCIA E GRAVAÇÃO LOCAL`, "warn");
        
        // Se a API moderna não for suportada, força o download nativo do Browser
        if (dirHandle === "MODO_DOWNLOAD_NATIVO") {
            addLog("A iniciar Download nativo. Por favor escolha a Pen USB na janela que vai abrir.", "info");
            
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
            
            addLog("Transferência solicitada ao navegador com sucesso.", "success");
            setTimeout(() => { document.getElementById('btn_format').disabled = false; }, 2000);
            return;
        }
        
        // Gravação direta silenciosa usando File System API
        document.getElementById('creation_progress').style.display = 'block';
        const progressBar = document.getElementById('progress_bar');
        const progressText = document.getElementById('progress_text');
        
        try {
            addLog(`A comunicar com o servidor da NAS...`, 'info');
            progressText.innerText = "A descarregar da NAS...";
            progressBar.style.width = "20%";
            
            const response = await fetch('/api/download_pkiris_api', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ficheiro: ficheiroSelecionadoPath })
            });
            
            if (!response.ok) throw new Error("O ficheiro não foi encontrado ou o servidor falhou.");
            
            progressBar.style.width = "60%";
            progressText.innerText = "A gravar na Pen USB...";
            
            const destName = document.getElementById('usb_drive_name').innerText;
            addLog(`Ficheiro transferido. A gravar fisicamente na unidade local: ${destName}...`, 'warn');
            
            const blob = await response.blob();
            
            // Criar e escrever o ficheiro no destino escolhido
            const fileHandle = await dirHandle.getFileHandle(nomeFicheiro, { create: true });
            const writable = await fileHandle.createWritable();
            await writable.write(blob);
            await writable.close();
            
            progressBar.style.width = "100%";
            progressText.innerText = "Gravação Concluída!";
            addLog(`Ficheiro ${nomeFicheiro} guardado e fechado na Pen com sucesso!`, 'success');
            addLog("A Pen Drive já pode ser removida do seu computador com segurança.", "success");
            addLog("==========================================", "warn");
            
        } catch (error) {
            addLog(`Erro durante o processo: ${error.message}`, 'error');
            progressText.innerText = "Ocorreu um erro.";
        }
        
        setTimeout(() => {
            document.getElementById('creation_progress').style.display = 'none';
            progressBar.style.width = "0%";
            validarBotaoCriacao();
        }, 5000);
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
    
    # Usa a leitura exata para garantir que vai ao ficheiro certo
    logo_ba = get_exact_image(DATA_DIR, "logo_ba.png")
    if not logo_ba:
        logo_ba = get_exact_image(BASE_DIR, "Logo_Green_Letters_white.png")
        
    logo_iris = get_exact_image(DATA_DIR, "logo_iris.png")
    
    return render_template_string(HTML_TEMPLATE, backups=backups, logo_ba=logo_ba, logo_iris=logo_iris)

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