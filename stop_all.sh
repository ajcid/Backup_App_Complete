#!/bin/bash
echo "=========================================="
echo "Parando Sistema Completo de Inspeção..."
echo "=========================================="

cd /volume1/docker/inspecao

# Parar processos principais
for pid_file in sistema_principal.pid mirror_ssd.pid backup_server.pid; do
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Parando processo (PID: $PID)..."
            kill "$PID"
            sleep 2
            # Force kill se necessário
            if kill -0 "$PID" 2>/dev/null; then
                echo "Forçando parada..."
                kill -9 "$PID" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    fi
done

# Parar todos os servidores de mosaico
echo "Parando servidores de mosaico..."
pkill -f "mosaic_complete.py" 2>/dev/null || true

# Remover arquivos PID de mosaicos
rm -f /volume1/docker/inspecao/mosaic_*.pid 2>/dev/null || true

echo "Sistema parado com sucesso"
echo "=========================================="
