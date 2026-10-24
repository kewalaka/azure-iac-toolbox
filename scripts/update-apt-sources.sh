#!/usr/bin/env bash
set -euo pipefail
: "${UPDATE_APT_STATE:?}"
mkdir -p "$UPDATE_APT_STATE/lists/partial"
curl -fsSL --retry 3 https://packages.microsoft.com/keys/microsoft.asc -o "$UPDATE_APT_STATE/microsoft.asc"
curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o "$UPDATE_APT_STATE/docker.asc"
for entry in 'microsoft BC528686B50D79E339D3721CEB3E94ADBE1229CF' 'docker 9DC858229FC7DD38854AE2D88D81803C0EBFCD88'; do
    read -r name fingerprint <<< "$entry"
    actual=$(gpg --batch --show-keys --with-colons "$UPDATE_APT_STATE/$name.asc" | awk -F: '$1 == "pub" {primary = 1} $1 == "fpr" && primary {print $10; primary = 0}')
    test "$actual" = "$fingerprint"
done
cat > "$UPDATE_APT_STATE/sources.list" <<EOF
deb [arch=amd64 signed-by=$UPDATE_APT_STATE/microsoft.asc] https://packages.microsoft.com/ubuntu/24.04/prod noble main
deb [arch=amd64 signed-by=$UPDATE_APT_STATE/microsoft.asc] https://packages.microsoft.com/repos/azure-cli/ noble main
deb [arch=amd64 signed-by=$UPDATE_APT_STATE/docker.asc] https://download.docker.com/linux/ubuntu noble stable
EOF
sudo apt-get -o "Dir::State::lists=$UPDATE_APT_STATE/lists" \
    -o "Dir::Etc::sourcelist=$UPDATE_APT_STATE/sources.list" -o Dir::Etc::sourceparts=- \
    -o APT::Update::Error-Mode=any update
