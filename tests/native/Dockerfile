ARG HA_IMAGE=ghcr.io/home-assistant/home-assistant:2026.10.2
FROM ${HA_IMAGE}
COPY dist/native/*.whl /opt/native-wheels/
RUN python - <<'PY'
import importlib.metadata as metadata
import json
import subprocess
from pathlib import Path
versions = {name: metadata.version(name) for name in ['rich', 'mcp', 'pydantic', 'httpx', 'cryptography']}
issues = subprocess.run(['python', '-m', 'pip', 'check'], capture_output=True, text=True).stdout.splitlines()
Path('/opt/native-dependencies.json').write_text(json.dumps({'before': versions, 'before_issues': issues}))
PY
RUN pip install --no-cache-dir -c /usr/src/homeassistant/homeassistant/package_constraints.txt /opt/native-wheels/*.whl
RUN python - <<'PY'
import importlib.metadata as metadata
import json
import subprocess
from pathlib import Path
p = Path('/opt/native-dependencies.json')
proof = json.loads(p.read_text())
proof['after'] = {name: metadata.version(name) for name in proof['before']}
proof['after_issues'] = subprocess.run(['python', '-m', 'pip', 'check'], capture_output=True, text=True).stdout.splitlines()
assert proof['before'] == proof['after'], proof
assert not (set(proof['after_issues']) - set(proof['before_issues'])), proof
p.write_text(json.dumps(proof))
PY
# Use the exact integration archive paired with the installed wheel.
COPY dist/native/ha_mcp_tools.zip /opt/native-component.zip
RUN python -c "import zipfile; zipfile.ZipFile('/opt/native-component.zip').extractall('/opt/native/custom_components/ha_mcp_tools')"
# Until a candidate is published, its HTTPS wheel URL does not exist yet.
# Only this disposable image pins the already-installed, byte-identical wheel;
# the release archive keeps the real immutable URL for HACS installation tests.
RUN python - <<'PY'
import importlib.metadata
import json
from pathlib import Path
p = Path('/opt/native/custom_components/ha_mcp_tools/manifest.json')
manifest = json.loads(p.read_text())
manifest['requirements'] = [
    'ha-mcp==' + importlib.metadata.version('ha-mcp')
    if requirement.startswith('ha-mcp @ ') else requirement
    for requirement in manifest['requirements']
]
p.write_text(json.dumps(manifest))
PY
COPY tests/native/fixtures/native_fixture /opt/native/custom_components/native_fixture
ENV PYTHONDONTWRITEBYTECODE=1
