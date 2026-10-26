# Build tests/native/Dockerfile's unpublished candidate first. Its explicit
# local-wheel substitution is why this lane NEVER claims HA-MCP self-update.
ARG CANDIDATE_IMAGE=ha-mcp-native-candidate
FROM ${CANDIDATE_IMAGE}
RUN python - <<'PY'
import hashlib, io, urllib.request, zipfile
from pathlib import Path
from homeassistant.util.package import install_package
url = 'https://github.com/hacs/integration/releases/download/2.0.5/hacs.zip'
data = urllib.request.urlopen(url, timeout=90).read()
assert hashlib.sha256(data).hexdigest() == '97be6b824a4f38e683728cc6dd72367f6b8bad0a43428b1b3b987a3087adf413'
with zipfile.ZipFile(io.BytesIO(data)) as bundle:
    bundle.extractall('/opt/native/custom_components/hacs')
assert install_package('aiogithubapi==26.0.0', constraints='/usr/src/homeassistant/homeassistant/package_constraints.txt')
PY
COPY tests/native/*.py /opt/qualification/native/
COPY tests/native/qualification_hacs/fixture /opt/native/custom_components/qualification_hacs_fixture
COPY tests/native/qualification_hacs/bootstrap.py tests/native/qualification_hacs/driver.py tests/native/qualification_hacs/checks.py /opt/qualification/
COPY dist/native/native-build.json /opt/qualification/native-build.json
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/opt/qualification/bootstrap.py"]
