ARG HA_IMAGE=ghcr.io/home-assistant/home-assistant:2026.10.2
FROM ${HA_IMAGE}
COPY dist/native/*.whl /opt/native-wheels/
RUN pip install --no-cache-dir -c /usr/src/homeassistant/homeassistant/package_constraints.txt /opt/native-wheels/*.whl
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
