FROM frappe/frappe:version-15

USER frappe
WORKDIR /home/frappe/frappe-bench

COPY --chown=frappe:frappe apps.json /tmp/apps.json

RUN python3 -c "
import json, subprocess, sys
apps = json.load(open('/tmp/apps.json'))
for app in apps:
    result = subprocess.run(
        ['bench', 'get-app', '--branch', app['branch'], app['url']],
        check=True
    )
"
