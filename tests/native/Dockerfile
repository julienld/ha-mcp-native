ARG HA_IMAGE=ghcr.io/home-assistant/home-assistant:2026.10.2
FROM ${HA_IMAGE} AS published
ARG SOURCE_REVISION
# Deliberately no wheel COPY or pip install: Core must fetch the public URL.
COPY scripts/native_published_assets.py /opt/native_published_assets.py
COPY dist/native/native-build.json dist/native/published-verification.json dist/native/ha_mcp_tools.zip /opt/native-assets/
RUN python /opt/native_published_assets.py prepare-image --assets /opt/native-assets --destination /opt/native/custom_components/ha_mcp_tools --source-revision "${SOURCE_REVISION}"
COPY tests/native/fixtures/native_fixture /opt/native/custom_components/native_fixture
RUN python - <<'PY'
import sys
sys.path.insert(0, '/opt/native/custom_components/native_fixture')
from dependency_receipt import capture_baseline
capture_baseline()
PY
ENV PYTHONDONTWRITEBYTECODE=1

# Last stage remains the default for unpublished source candidates.
FROM ${HA_IMAGE} AS candidate
COPY tests/native/fixtures/native_fixture/dependency_receipt.py /opt/native_dependency_receipt.py
COPY dist/native/*.whl /opt/native-wheels/
RUN python - <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, '/opt')
from native_dependency_receipt import capture_baseline, dependency_issues, installed_versions
# Exercise the published lane's baseline collector on real, untouched Core even
# before release assets exist.
capture_baseline(Path('/opt/public-baseline-smoke.json'))
Path('/opt/native-dependencies.json').write_text(json.dumps({'mode': 'candidate', 'before': installed_versions(), 'before_issues': dependency_issues()}))
PY
RUN python - <<'PY'
from pathlib import Path
from homeassistant.util.package import install_package
wheel, = Path('/opt/native-wheels').glob('*.whl')
assert install_package(str(wheel), upgrade=True, constraints='/usr/src/homeassistant/homeassistant/package_constraints.txt'), 'Core installer failed'
PY
RUN python - <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, '/opt')
from native_dependency_receipt import dependency_issues, installed_versions
p = Path('/opt/native-dependencies.json')
proof = json.loads(p.read_text())
after = installed_versions()
proof['after'] = {name: after.get(name) for name in proof['before']}
proof['added'] = {name: version for name, version in after.items() if name not in proof['before']}
proof['after_issues'] = dependency_issues()
changed = {name: {'before': version, 'after': after.get(name)} for name, version in proof['before'].items() if after.get(name) != version}
assert not changed, changed
new_issues = set(proof['after_issues']) - set(proof['before_issues'])
assert not new_issues, new_issues
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
