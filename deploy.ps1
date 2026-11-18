# Copy LuminAqua from this Windows PC to a Raspberry Pi and (re)install it. Needs SSH key login to the Pi.
#   .\deploy.ps1 -PiHost luminaqua.local          # update the app on a Pi that already has it
#   .\deploy.ps1 -PiHost 192.168.1.50 -Setup    # first time: OS update, Python env, service
#   -App <name>  folder in the Pi user's home and service name (default: luminaqua)
# (On the Pi itself you can instead: git clone ... && cd <repo>/luminaqua && bash setup-pi.sh)
param(
    [string]$PiHost = "luminaqua.local",
    [string]$User = "pi",
    [string]$App = "luminaqua",
    [switch]$Setup
)
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "luminaqua"
$t = "$User@$PiHost"

ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new $t "mkdir -p ~/$App/static"
scp -o BatchMode=yes -q "$src\server.py" "$src\protocol.py" "$src\requirements.txt" "$src\luminaqua.service" "$src\setup-pi.sh" "${t}:$App/"
scp -o BatchMode=yes -q "$src\static\index.html" "$src\static\favicon.png" "$src\static\apple-touch-icon.png" "$src\static\icon-192.png" "${t}:$App/static/"

if ($Setup) {
    # interactive (-t) so sudo can ask for the Pi's password
    ssh -t $t "sed -i 's/\r$//' ~/$App/setup-pi.sh && bash ~/$App/setup-pi.sh"
} else {
    ssh -o BatchMode=yes $t "sudo -n systemctl restart $App 2>/dev/null || sudo systemctl restart $App; sleep 3; systemctl is-active $App"
}
