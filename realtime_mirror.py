#!/usr/bin/env python3
"""
Mirror SSD em tempo real - Últimos 5 dias
Versão otimizada para performance e confiabilidade
Sistema de espelho automático com limpeza inteligente
"""
import os
import time
import shutil
import json
import threading
import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import psutil

# ==============================================================================
# CONFIGURAÇÃO DE CAMINHOS RELATIVOS E PORTÁTEIS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "backup_settings.json")
LOG_FILE = os.path.join(LOG_DIR, "mirror_ssd.log")
STATS_FILE = os.path.join(DATA_DIR, "mirror_stats.json")

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# Variável global para controle de parada
stop_event = threading.Event()

class MirrorHandler(FileSystemEventHandler):
    def __init__(self, src, dst, stats_tracker):
        super().__init__()
        self.src = Path(src)
        self.dst = Path(dst)
        self.stats = stats_tracker
        self.pending_files = set()
        self.last_mirror_time = {}

    def on_created(self, event):
        if event.is_directory: 
            return
        self._schedule_mirror(event.src_path)

    def on_modified(self, event):
        if event.is_directory: 
            return
        self._schedule_mirror(event.src_path)
    
    def on_moved(self, event):
        if event.is_directory:
            return
        # Quando um arquivo é movido, processar o destino
        self._schedule_mirror(event.dest_path)

    def _schedule_mirror(self, src_file):
        """Agendar espelhamento de arquivo (com debounce)"""
        try:
            # Verificar se é um arquivo válido
            if not self._is_valid_file(src_file):
                return
            
            # Debounce: evitar múltiplas operações no mesmo arquivo
            current_time = time.time()
            if src_file in self.last_mirror_time:
                if current_time - self.last_mirror_time[src_file] < 2:  # 2 segundos
                    return
            
            self.last_mirror_time[src_file] = current_time
            
            # Agendar para processamento
            if src_file not in self.pending_files:
                self.pending_files.add(src_file)
                # Processar em thread separada para não bloquear o watchdog
                threading.Thread(target=self._mirror_with_delay, args=(src_file,), daemon=True).start()
                
        except Exception as e:
            logging.error(f"Erro ao agendar espelhamento de {src_file}: {e}")

    def _mirror_with_delay(self, src_file):
        """Espelhar arquivo com pequeno delay para garantir que foi completamente escrito"""
        try:
            # Aguardar um pouco para arquivo ser completamente escrito
            time.sleep(1)
            
            if not os.path.exists(src_file):
                self.pending_files.discard(src_file)
                return
            
            # Verificar estabilidade do arquivo
            if not self._wait_for_stable_file(src_file):
                logging.warning(f"Arquivo instável ignorado: {src_file}")
                self.pending_files.discard(src_file)
                return
            
            # Executar espelhamento
            self._mirror(src_file)
            self.pending_files.discard(src_file)
            
        except Exception as e:
            logging.error(f"Erro no espelhamento com delay de {src_file}: {e}")
            self.pending_files.discard(src_file)

    def _wait_for_stable_file(self, filepath, max_wait=5):
        """Aguardar até arquivo estar estável"""
        try:
            last_size = -1
            stable_count = 0
            
            for _ in range(max_wait):
                if not os.path.exists(filepath):
                    return False
                
                current_size = os.path.getsize(filepath)
                if current_size == last_size and current_size > 0:
                    stable_count += 1
                    if stable_count >= 2:
                        return True
                else:
                    stable_count = 0
                
                last_size = current_size
                time.sleep(0.5)
            
            return current_size > 0  # Aceitar se tem conteúdo
            
        except Exception:
            return False

    def _is_valid_file(self, filepath):
        """Verificar se arquivo deve ser espelhado"""
        try:
            # Ignorar arquivos temporários e ocultos
            basename = os.path.basename(filepath)
            if basename.startswith('.') or basename.startswith('~') or basename.endswith('.tmp'):
                return False
            
            # Verificar extensões válidas
            valid_extensions = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.xml']
            if not any(filepath.lower().endswith(ext) for ext in valid_extensions):
                return False
            
            return True
            
        except Exception:
            return False

    def _mirror(self, src_file):
        """Espelhar arquivo individual"""
        try:
            start_time = time.time()
            
            # Calcular caminho relativo
            rel_path = Path(src_file).relative_to(self.src)
            dest_file = self.dst / rel_path
            
            # Criar diretório de destino
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Verificar se precisa copiar (comparar por tamanho e data)
            if dest_file.exists():
                src_stat = os.stat(src_file)
                dest_stat = os.stat(dest_file)
                
                # Se mesmo tamanho e data de modificação, pular
                if (src_stat.st_size == dest_stat.st_size and 
                    src_stat.st_mtime <= dest_stat.st_mtime):
                    logging.debug(f"Arquivo já atualizado: {rel_path}")
                    return
            
            # Copiar arquivo
            shutil.copy2(src_file, dest_file)
            
            # Atualizar estatísticas
            elapsed_time = time.time() - start_time
            file_size = os.path.getsize(src_file)
            
            self.stats.update_stats('files_mirrored', 1)
            self.stats.update_stats('bytes_mirrored', file_size)
            self.stats.update_stats('mirror_time', elapsed_time)
            
            logging.debug(f"Espelhado: {rel_path} ({file_size} bytes em {elapsed_time:.2f}s)")
            
        except Exception as e:
            self.stats.update_stats('mirror_errors', 1)
            logging.error(f"Erro no espelhamento de {src_file}: {e}")

class StatsTracker:
    """Rastreador de estatísticas do sistema"""
    
    def __init__(self):
        self.stats = {
            'start_time': datetime.now().isoformat(),
            'files_mirrored': 0,
            'bytes_mirrored': 0,
            'files_cleaned': 0,
            'cleanup_errors': 0,
            'mirror_errors': 0,
            'mirror_time': 0.0,
            'last_cleanup': None,
            'ssd_usage_percent': 0,
            'cleanup_runs': 0
        }
        self.lock = threading.Lock()
        
    def update_stats(self, key, value):
        """Atualizar estatística"""
        with self.lock:
            if key in ['files_mirrored', 'bytes_mirrored', 'files_cleaned', 
                      'cleanup_errors', 'mirror_errors', 'cleanup_runs']:
                self.stats[key] += value
            elif key == 'mirror_time':
                self.stats[key] += value
            else:
                self.stats[key] = value
    
    def get_stats(self):
        """Obter cópia das estatísticas"""
        with self.lock:
            return self.stats.copy()
    
    def save_stats(self):
        """Salvar estatísticas em arquivo"""
        try:
            with self.lock:
                stats_copy = self.stats.copy()
                stats_copy['last_update'] = datetime.now().isoformat()
                
            with open(STATS_FILE, 'w') as f:
                json.dump(stats_copy, f, indent=2)
                
        except Exception as e:
            logging.error(f"Erro ao salvar estatísticas: {e}")

def load_config():
    """Carregar configuração"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Erro ao carregar configuração: {e}")
    
    # Configuração padrão
    return {
        "source_path": "/volume1/inspecao_organizadas",
        "ssd_path": "/volume2/ssd_mirror",
        "ssd_retention_days": 5
    }

def get_ssd_usage(path):
    """Obter uso do SSD"""
    try:
        if os.path.exists(path):
            usage = shutil.disk_usage(path)
            percent = (usage.used / usage.total) * 100
            return {
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': percent
            }
    except Exception as e:
        logging.error(f"Erro ao obter uso do SSD: {e}")
    
    return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}

def cleanup_old_files(dst_path, retention_days, stats_tracker):
    """Limpar arquivos antigos do SSD"""
    logging.info(f"Iniciando limpeza de arquivos com mais de {retention_days} dias")
    
    try:
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed_count = 0
        removed_size = 0
        errors = 0
        
        # Caminhar por toda a estrutura
        for root, dirs, files in os.walk(dst_path):
            for file in files:
                if stop_event.is_set():
                    logging.info("Limpeza interrompida por sinal de parada")
                    return
                
                file_path = os.path.join(root, file)
                try:
                    # Verificar data de modificação
                    file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    
                    if file_time < cutoff:
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        removed_count += 1
                        removed_size += file_size
                        
                        logging.debug(f"Removido: {file_path}")
                        
                except Exception as e:
                    errors += 1
                    logging.error(f"Erro ao remover {file_path}: {e}")
        
        # Remover diretórios vazios
        try:
            remove_empty_directories(dst_path)
        except Exception as e:
            logging.error(f"Erro ao remover diretórios vazios: {e}")
        
        # Atualizar estatísticas
        stats_tracker.update_stats('files_cleaned', removed_count)
        stats_tracker.update_stats('cleanup_errors', errors)
        stats_tracker.update_stats('cleanup_runs', 1)
        stats_tracker.update_stats('last_cleanup', datetime.now().isoformat())
        
        logging.info(f"Limpeza concluída: {removed_count} arquivos removidos "
                    f"({removed_size / (1024*1024):.1f} MB), {errors} erros")
        
    except Exception as e:
        logging.error(f"Erro na limpeza: {e}")
        stats_tracker.update_stats('cleanup_errors', 1)

def remove_empty_directories(root_path):
    """Remover diretórios vazios recursivamente"""
    for root, dirs, files in os.walk(root_path, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            try:
                if not os.listdir(dir_path):  # Diretório vazio
                    os.rmdir(dir_path)
                    logging.debug(f"Diretório vazio removido: {dir_path}")
            except Exception as e:
                logging.debug(f"Não foi possível remover diretório {dir_path}: {e}")

def cleanup_loop(dst_path, retention_days, stats_tracker):
    """Loop de limpeza executado periodicamente"""
    cleanup_interval = 6 * 3600  # 6 horas
    
    while not stop_event.is_set():
        try:
            # Aguardar intervalo ou sinal de parada
            if stop_event.wait(cleanup_interval):
                break
            
            if stop_event.is_set():
                break
            
            # Executar limpeza
            cleanup_old_files(dst_path, retention_days, stats_tracker)
            
            # Atualizar uso do SSD
            ssd_usage = get_ssd_usage(dst_path)
            stats_tracker.update_stats('ssd_usage_percent', ssd_usage['percent'])
            
        except Exception as e:
            logging.error(f"Erro no loop de limpeza: {e}")
            time.sleep(60)  # Aguardar 1 minuto antes de tentar novamente

def stats_loop(stats_tracker):
    """Loop para salvar estatísticas periodicamente"""
    save_interval = 300  # 5 minutos
    
    while not stop_event.is_set():
        try:
            if stop_event.wait(save_interval):
                break
            
            stats_tracker.save_stats()
            
        except Exception as e:
            logging.error(f"Erro no loop de estatísticas: {e}")

def signal_handler(signum, frame):
    """Handler para sinais de sistema"""
    logging.info(f"Recebido sinal {signum}. Parando sistema...")
    stop_event.set()

def perform_initial_sync(src, dst, stats_tracker):
    """Realizar sincronização inicial se necessário"""
    logging.info("Verificando necessidade de sincronização inicial...")
    
    try:
        if not os.path.exists(dst):
            os.makedirs(dst, exist_ok=True)
            logging.info(f"Diretório SSD criado: {dst}")
        
        # Contar arquivos para sincronização inicial
        src_path = Path(src)
        if not src_path.exists():
            logging.warning(f"Pasta origem não existe: {src}")
            return
        
        # Verificar se há arquivos recentes que precisam ser sincronizados
        recent_cutoff = datetime.now() - timedelta(days=1)  # Último dia
        files_to_sync = []
        
        for root, dirs, files in os.walk(src):
            for file in files:
                if stop_event.is_set():
                    return
                
                file_path = os.path.join(root, file)
                try:
                    file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if file_time > recent_cutoff:
                        rel_path = os.path.relpath(file_path, src)
                        dest_file = os.path.join(dst, rel_path)
                        
                        # Verificar se precisa sincronizar
                        if not os.path.exists(dest_file):
                            files_to_sync.append(file_path)
                        
                except Exception as e:
                    logging.debug(f"Erro ao verificar {file_path}: {e}")
        
        if files_to_sync:
            logging.info(f"Sincronização inicial: {len(files_to_sync)} arquivos")
            
            for file_path in files_to_sync[:1000]:  # Limitar a 1000 arquivos por vez
                if stop_event.is_set():
                    break
                
                try:
                    rel_path = os.path.relpath(file_path, src)
                    dest_file = os.path.join(dst, rel_path)
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    shutil.copy2(file_path, dest_file)
                    
                    stats_tracker.update_stats('files_mirrored', 1)
                    stats_tracker.update_stats('bytes_mirrored', os.path.getsize(file_path))
                    
                except Exception as e:
                    logging.error(f"Erro na sincronização inicial de {file_path}: {e}")
        
        logging.info("Sincronização inicial concluída")
        
    except Exception as e:
        logging.error(f"Erro na sincronização inicial: {e}")

def main():
    """Função principal"""
    logging.info("=" * 60)
    logging.info("INICIANDO MIRROR SSD EM TEMPO REAL - v2.0")
    logging.info("Sistema de espelho automático com limpeza inteligente")
    logging.info("=" * 60)
    
    # Configurar handlers de sinal
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Carregar configuração
    config = load_config()
    src = config.get("source_path", "/volume1/inspecao_organizadas")
    dst = config.get("ssd_path", "/volume2/ssd_mirror")
    retention = config.get("ssd_retention_days", 5)
    
    logging.info(f"Configuração:")
    logging.info(f"  Origem: {src}")
    logging.info(f"  Destino SSD: {dst}")
    logging.info(f"  Retenção: {retention} dias")
    
    # Inicializar rastreador de estatísticas
    stats_tracker = StatsTracker()
    
    # Criar pasta de destino
    try:
        Path(dst).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error(f"Erro ao criar pasta SSD: {e}")
        sys.exit(1)
    
    # Verificar se pasta origem existe
    if not os.path.exists(src):
        logging.warning(f"Pasta origem não existe: {src}")
        logging.info("Sistema continuará monitorando...")
    
    # Realizar sincronização inicial
    perform_initial_sync(src, dst, stats_tracker)
    
    # Configurar handler do mirror
    handler = MirrorHandler(src, dst, stats_tracker)
    observer = Observer()
    
    try:
        observer.schedule(handler, src, recursive=True)
    except Exception as e:
        logging.error(f"Erro ao configurar observador: {e}")
        sys.exit(1)
    
    # Iniciar threads de limpeza e estatísticas
    cleanup_thread = threading.Thread(
        target=cleanup_loop, 
        args=(dst, retention, stats_tracker), 
        daemon=True
    )
    cleanup_thread.start()
    
    stats_thread = threading.Thread(
        target=stats_loop,
        args=(stats_tracker,),
        daemon=True
    )
    stats_thread.start()
    
    # Iniciar observador
    try:
        observer.start()
        logging.info("Mirror SSD ativo e monitorando:")
        logging.info(f"  {src} -> {dst}")
        logging.info(f"  Retenção: {retention} dias")
        logging.info("Sistema pronto!")
        
        # Loop principal
        while not stop_event.is_set():
            try:
                time.sleep(30)
                
                # Log de status periódico
                stats = stats_tracker.get_stats()
                if stats['files_mirrored'] > 0:
                    avg_time = stats['mirror_time'] / stats['files_mirrored']
                    logging.info(f"Status: {stats['files_mirrored']} arquivos espelhados, "
                               f"média {avg_time:.2f}s por arquivo")
                
            except KeyboardInterrupt:
                break
    
    except Exception as e:
        logging.error(f"Erro crítico: {e}")
    
    finally:
        logging.info("Parando mirror SSD...")
        stop_event.set()
        
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=10)
        
        # Salvar estatísticas finais
        stats_tracker.save_stats()
        final_stats = stats_tracker.get_stats()
        
        logging.info("Estatísticas finais:")
        logging.info(f"  Arquivos espelhados: {final_stats['files_mirrored']}")
        logging.info(f"  Bytes espelhados: {final_stats['bytes_mirrored'] / (1024*1024):.1f} MB")
        logging.info(f"  Arquivos limpos: {final_stats['files_cleaned']}")
        logging.info(f"  Execuções de limpeza: {final_stats['cleanup_runs']}")
        logging.info(f"  Erros de espelhamento: {final_stats['mirror_errors']}")
        logging.info(f"  Erros de limpeza: {final_stats['cleanup_errors']}")
        
        logging.info("Mirror SSD parado")
        logging.info("=" * 60)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.error(f"Erro fatal: {e}")
        sys.exit(1)