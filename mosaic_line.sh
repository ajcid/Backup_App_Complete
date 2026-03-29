#!/bin/bash
# Controlo individual das linhas de mosaico
# Uso: mosaic_line.sh <linha> start|stop|status|restart

if [ $# -ne 2 ]; then
    echo "Uso: $0 <linha> start|stop|status|restart"
    echo "Exemplo: $0 21 start"
    exit 1
fi

linha=$1
acao=$2
porta=$((8000+linha))
pid_file="/volume1/docker/inspecao/mosaic_${linha}.pid"
log_file="/volume1/logs/mosaic_${linha}.log"

# Validar linha
if ! [[ "$linha" =~ ^(21|22|23|24|31|32|33|34)$ ]]; then
    echo "Erro: Linha inválida '$linha'. Use: 21, 22, 23, 24, 31, 32, 33, 34"
    exit 1
fi

case "$acao" in
    start)
        if [ -f "$pid_file" ] && kill -0 $(cat "$pid_file") 2>/dev/null; then
            echo "Linha $linha já está em execução (PID: $(cat "$pid_file"), Porta: $porta)"
            exit 0
        fi
        
        cd /volume1/docker/inspecao
        if [ ! -f "mosaic_complete.py" ]; then
            echo "Erro: mosaic_complete.py não encontrado"
            exit 1
        fi
        
        echo "Iniciando linha $linha na porta $porta..."
        nohup python3 mosaic_complete.py "$linha" > "$log_file" 2>&1 &
        echo $! > "$pid_file"
        sleep 1
        
        if kill -0 $(cat "$pid_file") 2>/dev/null; then
            echo "Linha $linha iniciada com sucesso (PID: $(cat "$pid_file"))"
            echo "Acesse: http://$(hostname -I | awk '{print $1}'):$porta"
        else
            echo "Erro ao iniciar linha $linha"
            rm -f "$pid_file"
            exit 1
        fi
        ;;
    stop)
        if [ -f "$pid_file" ]; then
            PID=$(cat "$pid_file")
            if kill -0 "$PID" 2>/dev/null; then
                echo "Parando linha $linha (PID: $PID)..."
                kill "$PID"
                sleep 2
                if kill -0 "$PID" 2>/dev/null; then
                    echo "Forçando parada..."
                    kill -9 "$PID" 2>/dev/null || true
                fi
                echo "Linha $linha parada"
            else
                echo "Linha $linha não estava em execução"
            fi
            rm -f "$pid_file"
        else
            echo "Linha $linha não está em execução"
        fi
        ;;
    restart)
        echo "Reiniciando linha $linha..."
        $0 "$linha" stop
        sleep 2
        $0 "$linha" start
        ;;
    status)
        if [ -f "$pid_file" ]; then
            PID=$(cat "$pid_file")
            if kill -0 "$PID" 2>/dev/null; then
                echo "Linha $linha: EXECUTANDO (PID: $PID, Porta: $porta)"
                echo "URL: http://$(hostname -I | awk '{print $1}'):$porta"
            else
                echo "Linha $linha: PARADA (PID file inválido)"
                rm -f "$pid_file"
            fi
        else
            echo "Linha $linha: PARADA"
        fi
        ;;
    *)
        echo "Ação inválida: $acao"
        echo "Use: start|stop|status|restart"
        exit 1
        ;;
esac
