#!/usr/bin/env bash
# Repair incomplete Docker iptables so user-defined compose bridges can
# talk to each other and reach the internet. No-op on Docker Desktop.
set -euo pipefail
if ! command -v iptables >/dev/null 2>&1 && ! command -v iptables-legacy >/dev/null 2>&1; then
  echo "no iptables; skipping (likely Docker Desktop)"
  exit 0
fi
IPT=${IPTABLES:-}
if [[ -z "$IPT" ]]; then
  if iptables-legacy -L DOCKER-USER -n >/dev/null 2>&1; then
    IPT=iptables-legacy
  else
    IPT=iptables
  fi
fi
$IPT -C DOCKER-USER -i br-+ -o br-+ -j ACCEPT 2>/dev/null || $IPT -I DOCKER-USER -i br-+ -o br-+ -j ACCEPT
$IPT -C DOCKER-USER -i br-+ ! -o br-+ -j ACCEPT 2>/dev/null || $IPT -I DOCKER-USER -i br-+ ! -o br-+ -j ACCEPT
$IPT -C DOCKER-USER -o br-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  $IPT -I DOCKER-USER -o br-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
# MASQUERADE any compose /16 that is not docker0's 172.17.0.0/16
for net in $(docker network ls --format '{{.Name}}' | grep -v '^bridge$' | grep -v '^host$' | grep -v '^none$' || true); do
  subnet=$(docker network inspect "$net" -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null || true)
  [[ -z "$subnet" || "$subnet" == 172.17.* ]] && continue
  $IPT -t nat -C POSTROUTING -s "$subnet" ! -o docker0 -j MASQUERADE 2>/dev/null || \
    $IPT -t nat -A POSTROUTING -s "$subnet" ! -o docker0 -j MASQUERADE
done
echo "compose-bridge iptables ok ($IPT)"
