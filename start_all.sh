#!/bin/bash
echo "=========================================="
echo "Iniciando Sistema Completo de Inspeção..."
echo "=========================================="

# 1. Torna o script portátil: deteta a pasta atual e entra nela
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"
echo "Trabalhando no diretório local: $DIR"

# 2. Cria a pasta local de logs caso não exista
mkdir -p data/logs

# Verificar se já está em execução
if [ -f sistema_principal.pid ] && kill -0 $(cat sistema_principal.pid) 2>/dev/null; then
    echo "Sistema principal já está em execução"
else
    # Sistema principal de monitorização
    echo "Iniciando sistema principal..."
    if [ -f "inspecao_synology.py" ]; then
        nohup python3 inspecao_synology.py > data/logs/sistema_principal.log 2>&1 &
        echo $! > sistema_principal.pid
        echo "Sistema principal iniciado (PID: $(cat sistema_principal.pid))"
    else
        echo "Ficheiro inspecao_synology.py não encontrado, a ignorar."
    fi
fi

# Mirror SSD (se volume2 existir)
if [ -d "/volume2" ]; then
    if [ -f mirror_ssd.pid ] && kill -0 $(cat mirror_ssd.pid) 2>/dev/null; then
        echo "Mirror SSD já está em execução"
    else
        echo "Iniciando mirror SSD..."
        if [ -f "realtime_mirror.py" ]; then
            nohup python3 realtime_mirror.py > data/logs/mirror_ssd.log 2>&1 &
            echo $! > mirror_ssd.pid
            echo "Mirror SSD iniciado (PID: $(cat mirror_ssd.pid))"
        else
            echo "Ficheiro realtime_mirror.py não encontrado, a ignorar."
        fi
    fi
else
    echo "Volume2 não encontrado - Mirror SSD desabilitado"
fi

# Servidor de backup e gestão web
if [ -f backup_server.pid ] && kill -0 $(cat backup_server.pid) 2>/dev/null; then
    echo "Servidor de backup já está em execução"
else
    echo "Iniciando servidor de backup..."
    if [ -f "backup_app_complete.py" ]; then
        nohup python3 backup_app_complete.py > data/logs/backup_server.log 2>&1 &
        echo $! > backup_server.pid
        echo "Servidor de backup iniciado (PID: $(cat backup_server.pid))"
    else
        echo "Ficheiro backup_app_complete.py não encontrado, a ignorar."
    fi
fi

sleep 2

echo "=========================================="
echo "Sistema iniciado com sucesso a partir de $DIR!"
echo "=========================================="
echo "Interfaces disponíveis:"
echo "- Backup/Diagnóstico: http://$(hostname -I | awk '{print $1}'):5580"
echo "- Mosaicos individuais: http://$(hostname -I | awk '{print $1}'):8021-8034"
echo ""
echo "Comandos úteis:"
echo "- start-inspection  (iniciar sistema)"
echo "- stop-inspection   (parar sistema)"
echo "- Logs: tail -f data/logs/*.log"
echo "=========================================="