#!/usr/bin/env python3
"""Print only systemd lifecycle facts for recent isolated simulation units."""
import json
import os
import re
import subprocess

assert os.geteuid() == 0
result = subprocess.run(['/usr/bin/journalctl', '--no-pager', '--output=json', '-n', '80',
    '--since=15 minutes ago', '-u', 'astrbot-wechat-onebot-sim-*.service'], capture_output=True, timeout=5)
assert result.returncode == 0 and len(result.stdout) < 512*1024
rows=[]
for raw in result.stdout.splitlines():
    row=json.loads(raw)
    if row.get('_COMM') != 'systemd': continue
    name=row.get('UNIT','')
    if not re.fullmatch('astrbot-wechat-onebot-sim-[0-9a-f]+.service',name): continue
    message=row.get('MESSAGE','')
    match=re.search(r'(Failed with result .[a-z-]+.|Main process exited, code=[a-z-]+, status=[A-Za-z0-9_/]+|'
                    r'Service runtime .*|Consumed .*CPU time.*|.*reached.*|.*killed by the OOM killer.*)',message)
    if match:
        rows.append({'unit':name,'event':match[0][:240]})
print(json.dumps({'stage':'simulation_lifecycle','events':rows}))
