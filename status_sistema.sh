#!/bin/bash
echo "=========================================="
echo "STATUS DO SISTEMA DE INSPEÇÃO"
echo "=========================================="

cd /volume1/docker/inspecao

# Verificar serviços principais
echo "SERVIÇOS PRINCIPAIS:"

services=("sistema_principal" "mirror_ssd" "backup_server")
for service in "${services[@]}"; do
    pid_file="${service}.pid"
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            echo "  ✓ $service: EXECUTANDO (PID: $PID)"
        else
            echo "  ✗ $service: PARADO (PID inválido)"
        fi
    else
        echo "  ✗ $service: PARADO"
    fi
done

# Verificar mosaicos
echo ""
echo "SERVIDORES DE MOSAICO:"
for linha in 21 22 23 24 31 32 33 34; do
    pid_file="mosaic_${linha}.pid"
    porta=$((8000+linha))
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            echo "  ✓ Linha $linha: EXECUTANDO (PID: $PID, Porta: $porta)"
        else
            echo "  ✗ Linha $linha: PARADO"
        fi
    else
        echo "  ✗ Linha $linha: PARADO"
    fi
done

# Verificar conectividade
echo ""
echo "CONECTIVIDADE:"
if command -v netstat >/dev/null 2>&1; then
    backup_port=$(netstat -ln 2>/dev/null | grep ":5580" | wc -l)
    if [ "$backup_port" -gt 0 ]; then
        echo "  ✓ Interface de backup: http://$(hostname -I | awk '{print $1}'):5580"
    else
        echo "  ✗ Interface de backup: Não acessível"
    fi
else
    echo "  ? Verificação de rede: netstat não disponível"
fi

echo "=========================================="
