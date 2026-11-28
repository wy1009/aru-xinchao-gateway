"""Keep Aru's already-configured Hindsight MCP URL working across cutover to Serein."""
from pathlib import Path
import os
from datetime import datetime, timezone
import shutil
import subprocess
import tempfile


def install(path=Path('/etc/caddy/Caddyfile')):
    before = path.read_text()
    start = before.index('    @hindsight_mcp path ')
    end = before.index('    @hindsight_panel {',start)
    block = before[start:end]
    after = before
    if 'rewrite * /serein/mcp' not in block or '127.0.0.1:18217' not in block:
        if block.count('rewrite * /mcp/gushou/?') != 1 or block.count('import /etc/caddy/hindsight-mcp-upstream.caddy') != 1:
            raise ValueError('Unexpected old MCP route')
        prefix = block[:block.index('            rewrite * /mcp/gushou/?')]
        new = prefix + """            rewrite * /serein/mcp
            reverse_proxy 127.0.0.1:18217 {
                flush_interval -1
                header_down Cache-Control "private, no-store"
            }
        }
    }
"""
        after = before[:start] + new + before[end:]
    panel_start = after.index('    handle @hindsight_panel {')
    panel_end = after.index('    redir /__serein ',panel_start)
    old_panel = after[panel_start:panel_end]
    if 'reverse_proxy 127.0.0.1:18460' not in old_panel and '/__serein/#materials' not in old_panel:
        raise ValueError('Unexpected Hindsight panel route')
    after = after[:panel_start] + '    handle @hindsight_panel {\n        redir * "/__serein/#materials" 302\n    }\n' + after[panel_end:]
    if after == before:
        return
    pid = subprocess.check_output(['systemctl','show','caddy','-p','MainPID','--value'],text=True).strip()
    env = dict(x.split(b'=',1) for x in Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in x)
    def caddy(action, target):
        subprocess.run(['/usr/local/bin/caddy',action,'--config',str(target),'--adapter','caddyfile'],
                       check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backups = Path('/var/backups/aru-serein-library')
    backups.mkdir(mode=0o700,exist_ok=True)
    backup = backups / ('Caddyfile-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    shutil.copy2(path,backup)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        pending = Path(f.name)
        os.fchmod(f.fileno(),path.stat().st_mode & 0o777)
        os.fchown(f.fileno(),path.stat().st_uid,path.stat().st_gid)
        f.write(after.encode())
    try:
        caddy('validate',pending)
        if before != path.read_text():
            raise ValueError('Caddy configuration changed')
        pending.replace(path)
        try:
            caddy('reload',path)
        except Exception:
            shutil.copy2(backup,path)
            caddy('reload',path)
            raise
    finally:
        pending.unlink(missing_ok=True)


if __name__=='__main__':
    install()
