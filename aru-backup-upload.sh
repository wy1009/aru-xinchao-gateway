#!/bin/bash
# Run "open" on the VPS. The temporary service receives one archive for the configured collaborator.
set -euo pipefail
gw=/opt/xinchao-nian/gateway
python=/opt/xinchao-nian/venv/bin/python
base=${ARU_PUBLIC_ORIGIN:-}
admin=http://127.0.0.1:2019
mode=${1:-open}
slot=${2:-}
if [[ $mode == open ]]; then
    slot=aru-upload-$(openssl rand -hex 8)
elif [[ ! $slot =~ ^aru-upload-[0-9a-f]{16}$ ]]; then
    exit 2
fi
unit="$slot.service"
dir="/var/lib/$slot"
close_route() { curl -sS --max-time 6 -X DELETE "$admin/id/$slot" -o /dev/null || true; }
cleanup() {
    close_route
    systemctl stop "$unit" 2>/dev/null || true
    # This path is dedicated to this single-use systemd StateDirectory.
    rm -f -- "$dir/archive.polarisarchive" "$dir/upload.part"
    if [[ -d $dir ]]; then
        find -H "$dir" -maxdepth 1 -type d -name 'aru-read-*' -exec rm -rf -- {} +
    fi
    if [[ -L $dir ]]; then unlink "$dir"; fi
    rmdir "/var/lib/private/$slot" 2>/dev/null || true
    rmdir "$dir" 2>/dev/null || true
    rm -f -- "/run/$slot/aru_inbox.py"
    rmdir "/run/$slot" 2>/dev/null || true
}
if [[ $mode == receive ]]; then
    trap cleanup EXIT
    for ((i=0; i<2400; i++)); do
        if [[ -f $dir/archive.polarisarchive ]]; then
            close_route
            "$python" -B "$gw/aru_backup.py" "$dir/archive.polarisarchive" --apply --consume
            exit 0
        fi
        sleep 3
    done
    exit 0
elif [[ $mode == close ]]; then
    cleanup
    exit 0
elif [[ $mode != open ]]; then
    exit 2
fi
: "${base:?set ARU_PUBLIC_ORIGIN to the public HTTPS origin}"
key=$(openssl rand -hex 32)
hash=$(printf '%s' "$key" | sha256sum | cut -d' ' -f1)
port=$("$python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
trap cleanup ERR
install -d -m 0755 "/run/$slot"
install -m 0644 "$gw/aru_inbox.py" "/run/$slot/aru_inbox.py"
systemd-run --quiet --collect --unit="$slot" --property=DynamicUser=yes --property=StateDirectory="$slot" \
    --property=StateDirectoryMode=0700 --property=UMask=0077 --property=ProtectSystem=strict --property=ProtectHome=yes \
    --property=NoNewPrivileges=yes --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv="INBOX_PATH=/$slot" --setenv="INBOX_DIGEST=$hash" \
    /opt/xinchao-nian/venv/bin/uvicorn --app-dir "/run/$slot" aru_inbox:app --host 127.0.0.1 --port "$port" --no-access-log
for ((i=0;i<50;i++)); do
    if curl -fs -o /dev/null "http://127.0.0.1:$port/$slot"; then break; fi
    sleep .2
done
curl -fs -o /dev/null "http://127.0.0.1:$port/$slot"
"$python" - "$slot" "$port" "$base" <<'ROUTE'
import json,sys,urllib.request
from urllib.parse import urlsplit
slot,port,base=sys.argv[1:]
url=urlsplit(base)
if url.scheme != "https" or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
    raise ValueError("Expected a public HTTPS origin")
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
servers='http://127.0.0.1:2019/config/apps/http/servers/'
with op.open(servers) as response:
    public,=[name for name,config in json.load(response).items() if ':443' in config.get('listen',[])]
route={'@id':slot,'match':[{'host':[url.hostname],'path':['/'+slot,'/'+slot+'/upload']}],
       'handle':[{'handler':'reverse_proxy','upstreams':[{'dial':'127.0.0.1:'+port}]}],'terminal':True}
req=urllib.request.Request(servers+public+'/routes/0',data=json.dumps(route).encode(),method='PUT',headers={'Content-Type':'application/json'})
with op.open(req) as response: assert response.status==200
ROUTE
curl --noproxy '*' -fs -o /dev/null "$base/$slot"
systemd-run --quiet --collect --unit="$slot-process" --property=RuntimeMaxSec=3h --setenv="ARU_COLLABORATOR_ID=${ARU_COLLABORATOR_ID:?set ARU_COLLABORATOR_ID}" --setenv="ARU_COLLABORATOR_NAME=${ARU_COLLABORATOR_NAME:-小禾}" \
    --property="ExecStopPost=/bin/bash $gw/aru-backup-upload.sh close $slot" \
    /bin/bash "$gw/aru-backup-upload.sh" receive "$slot"
trap - ERR
printf '%s/%s#%s\n' "$base" "$slot" "$key"
