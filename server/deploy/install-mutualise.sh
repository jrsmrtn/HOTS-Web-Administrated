#!/usr/bin/env bash
# HOTS Hosts web console on shared hosting or a Virtualmin / Webmin virtual server.
#
#   No root, no sudo, no apt: everything goes into ~/hots-console and runs as your own user.
#   - Python 3.11+ already on the server, or downloaded into ~/hots-console (with uv) if missing;
#   - libraries shipped in the zip (vendor/): no pip needed;
#   - started by your crontab (@reboot + watchdog every 5 minutes) and a daily backup;
#   - published on your domain through Apache (.htaccess proxy written for you) or the
#     "Proxy paths" of your panel. HTTPS: the Let's Encrypt certificate of your panel.
#
# Usage (from the unzipped HOTS_Console_Server folder):
#   ./install-mutualise.sh                       questions
#   ./install-mutualise.sh --domain console.example.fr --yes
#   options: --domain D  --webroot DIR  --port N  --dir DIR  --no-htaccess  --no-cron  --yes
#
# Afterwards: ~/hots-console/hots  start | stop | restart | status | logs | setup-link |
#                                   reset-password EMAIL | backup | info | uninstall
set -euo pipefail
umask 077

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${HOME}/hots-console"
DOMAIN=""
WEBROOT=""
PORT=""
HTACCESS=1
CRON=1
YES=0

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mErreur : %s\033[0m\n' "$*" >&2; exit 1; }
ask() {  # ask VAR "question" "default"
  local __var="$1" __q="$2" __def="${3:-}" __ans=""
  if [ "${YES}" = 1 ]; then printf -v "${__var}" '%s' "${__def}"; return; fi
  if [ -n "${__def}" ]; then read -r -p "${__q} [${__def}] : " __ans || true; else read -r -p "${__q} : " __ans || true; fi
  printf -v "${__var}" '%s' "${__ans:-${__def}}"
}
confirm() {  # confirm "question" (default yes)
  local ans=""
  [ "${YES}" = 1 ] && return 0
  read -r -p "$1 [O/n] : " ans || true
  case "${ans}" in n|N|non|Non|no|No) return 1 ;; *) return 0 ;; esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --webroot) WEBROOT="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --no-htaccess) HTACCESS=0; shift ;;
    --no-cron) CRON=0; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "option inconnue : $1 (voir --help)" ;;
  esac
done

[ -d "${SRC}/hots_server" ] || die "lancez ce script depuis le dossier HOTS_Console_Server décompressé."
if [ "$(id -u)" = 0 ]; then
  say "Attention : vous êtes root. Sur un VPS à vous, préférez « sudo ./deploy.sh » (installation complète)."
  confirm "Continuer quand même avec l'installation sans privilèges ?" || exit 1
fi

say "Installation de la console HOTS Hosts sans droits administrateur"
say "Dossier d'installation : ${DIR}"

# --- 1. Python 3.11+ ---------------------------------------------------------------------------------
step "Recherche de Python 3.11 ou plus récent"
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
PY=""
if [ -x "${DIR}/python/current/bin/python3" ] && py_ok "${DIR}/python/current/bin/python3"; then
  PY="${DIR}/python/current/bin/python3"
fi
if [ -z "${PY}" ]; then
  for c in python3.13 python3.12 python3.11 python3 \
           /opt/alt/python313/bin/python3 /opt/alt/python312/bin/python3 /opt/alt/python311/bin/python3 \
           /usr/local/bin/python3; do
    p="$(command -v "${c}" 2>/dev/null || true)"
    if [ -n "${p}" ] && py_ok "${p}"; then PY="${p}"; break; fi
  done
fi
if [ -z "${PY}" ]; then
  say "Aucun Python 3.11+ sur ce serveur : téléchargement d'un Python privé dans ${DIR}/python (environ 30 Mo)."
  confirm "Télécharger Python ?" || die "Python 3.11+ est nécessaire."
  mkdir -p "${DIR}/bin" "${DIR}/python"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="${DIR}/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="${DIR}/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
  else
    die "ni curl ni wget : impossible de télécharger Python. Demandez Python 3.11+ à votre hébergeur."
  fi
  UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_DIR="${DIR}/python/store" "${DIR}/bin/uv" python install 3.12 >/dev/null \
    || die "le téléchargement de Python a échoué (accès Internet sortant bloqué ?)."
  found="$(UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_DIR="${DIR}/python/store" "${DIR}/bin/uv" python find 3.12)"
  ln -sfn "$(dirname "$(dirname "${found}")")" "${DIR}/python/current"
  PY="${DIR}/python/current/bin/python3"
  [ -x "${PY}" ] || PY="${found}"
fi
say "Python : ${PY} ($("${PY}" -c 'import platform; print(platform.python_version())'))"

# --- 2. Application files -----------------------------------------------------------------------------
step "Copie de l'application"
mkdir -p "${DIR}/data" "${DIR}/backups" "${DIR}/logs" "${DIR}/run"
chmod 700 "${DIR}" "${DIR}/data" "${DIR}/backups"
rm -rf "${DIR}/app.new"
mkdir -p "${DIR}/app.new"
cp -R "${SRC}/hots_server" "${DIR}/app.new/"
[ -f "${SRC}/requirements.txt" ] && cp "${SRC}/requirements.txt" "${DIR}/app.new/"
[ -f "${SRC}/VERSION" ] && cp "${SRC}/VERSION" "${DIR}/app.new/"
if [ -d "${SRC}/vendor" ]; then
  cp -R "${SRC}/vendor" "${DIR}/app.new/vendor"
else
  say "Bibliothèques absentes du dossier : installation avec pip dans le dossier de l'application."
  "${PY}" -m pip install --quiet --disable-pip-version-check --target "${DIR}/app.new/vendor" \
    -r "${DIR}/app.new/requirements.txt" || die "pip a échoué. Utilisez le zip publié sur GitHub (bibliothèques incluses)."
fi
find "${DIR}/app.new" -name '__pycache__' -prune -exec rm -rf {} +
PYTHONPATH="${DIR}/app.new/vendor:${DIR}/app.new" "${PY}" -c 'import hots_server.app' \
  || die "l'application ne démarre pas avec ${PY}."
WAS_RUNNING=0
if [ -x "${DIR}/hots" ] && "${DIR}/hots" running >/dev/null 2>&1; then WAS_RUNNING=1; "${DIR}/hots" stop || true; fi
rm -rf "${DIR}/app.old"
[ -d "${DIR}/app" ] && mv "${DIR}/app" "${DIR}/app.old"
mv "${DIR}/app.new" "${DIR}/app"
rm -rf "${DIR}/app.old"

# --- 3. Settings ---------------------------------------------------------------------------------------
step "Réglages"
ENV_FILE="${DIR}/hots.env"
old_get() { if [ -f "${ENV_FILE}" ]; then sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1; fi; }
[ -n "${DOMAIN}" ] || DOMAIN="$(old_get HOTS_PUBLIC_URL | sed 's#^https://##')"
[ -n "${PORT}" ] || PORT="$(old_get HOTS_PORT)"
if [ -z "${DOMAIN}" ]; then
  guess=""
  for d in "${HOME}"/domains/*/public_html; do
    [ -d "${d}" ] && guess="$(basename "$(dirname "${d}")")" && break
  done
  ask DOMAIN "Adresse de la console (nom de domaine ou sous-domaine, sans https://)" "${guess}"
fi
DOMAIN="$(printf '%s' "${DOMAIN}" | sed -e 's#^https\?://##' -e 's#/.*$##' | tr '[:upper:]' '[:lower:]')"
printf '%s' "${DOMAIN}" | grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?$' || die "adresse invalide : « ${DOMAIN} »"
if [ -z "${PORT}" ]; then
  PORT="$("${PY}" - <<'PYEOF'
import random, socket
for _ in range(200):
    port = random.randint(20000, 39999)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
PYEOF
)"
fi
[ -n "${PORT}" ] || die "aucun port local libre trouvé."
cat > "${ENV_FILE}" <<EOF
# Console HOTS Hosts (hébergement mutualisé). Modifiez puis : ${DIR}/hots restart
HOTS_PORT=${PORT}
HOTS_PUBLIC_URL=https://${DOMAIN}
HOTS_DATA_DIR=${DIR}/data
HOTS_COOKIE_SECURE=1
HOTS_AGENT_LANG=fr
HOTS_PYTHON=${PY}
EOF
chmod 600 "${ENV_FILE}"
say "Console : https://${DOMAIN}  (service local : 127.0.0.1:${PORT})"

# --- 4. Control script -----------------------------------------------------------------------------------
cat > "${DIR}/hots" <<'EOF'
#!/usr/bin/env bash
# Console HOTS Hosts : start | stop | restart | status | logs | setup-link | reset-password EMAIL |
#                      set-email ANCIENNE NOUVELLE | backup | info | watchdog | uninstall
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; . "${DIR}/hots.env"; set +a
export PYTHONPATH="${DIR}/app/vendor:${DIR}/app"
PIDFILE="${DIR}/run/hots.pid"
LOG="${DIR}/logs/hots.log"
cd "${DIR}/app" || exit 1

running() { [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; }
healthy() {
  "${HOTS_PYTHON}" - "${HOTS_PORT}" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=5).read()
PYEOF
}
start() {
  if running; then echo "Déjà démarrée (PID $(cat "${PIDFILE}"))."; return 0; fi
  if [ -f "${LOG}" ] && [ "$(wc -c < "${LOG}")" -gt 5000000 ]; then mv -f "${LOG}" "${LOG}.1"; fi
  nohup "${HOTS_PYTHON}" -m hots_server serve --host 127.0.0.1 --port "${HOTS_PORT}" >> "${LOG}" 2>&1 < /dev/null &
  echo $! > "${PIDFILE}"
  for _ in $(seq 1 30); do
    if healthy; then echo "Console démarrée : ${HOTS_PUBLIC_URL}"; return 0; fi
    running || break
    sleep 1
  done
  echo "La console ne répond pas. Dernières lignes du journal :"; tail -n 20 "${LOG}"; return 1
}
stop() {
  if running; then
    kill "$(cat "${PIDFILE}")" 2>/dev/null
    for _ in $(seq 1 20); do running || break; sleep 0.5; done
    running && kill -9 "$(cat "${PIDFILE}")" 2>/dev/null
  fi
  rm -f "${PIDFILE}"
}
cli() { "${HOTS_PYTHON}" -m hots_server "$@"; }

case "${1:-status}" in
  start) start ;;
  stop) stop; echo "Console arrêtée." ;;
  restart) stop; start ;;
  running) running ;;
  status)
    if running && healthy; then echo "En marche (PID $(cat "${PIDFILE}")) : ${HOTS_PUBLIC_URL}"; cli info
    elif running; then echo "Processus présent mais la console ne répond pas : $0 restart"; exit 1
    else echo "Arrêtée : $0 start"; exit 1; fi ;;
  watchdog)  # crontab, every 5 minutes: restart if stopped or stuck
    if ! running; then start >/dev/null
    elif ! healthy; then sleep 10; healthy || { stop; start >/dev/null; }; fi ;;
  logs) tail -n 100 -f "${LOG}" ;;
  setup-link)
    token="$(cli setup-link 2>/dev/null | tail -n 1)"
    if [ -n "${token}" ] && [ "${token}" != "already-configured" ]; then echo "${HOTS_PUBLIC_URL}/#/setup/${token}"
    else echo "Console déjà configurée : ${HOTS_PUBLIC_URL}"; fi ;;
  reset-password) shift; cli reset-password "$@" ;;
  set-email) shift; cli set-email "$@" ;;
  info) cli info ;;
  backup) cli backup "${DIR}/backups" --keep 14 ;;
  uninstall)
    read -r -p "Supprimer la console, sa base de données et ses sauvegardes ? Tapez SUPPRIMER : " ans
    [ "${ans}" = "SUPPRIMER" ] || { echo "Annulé."; exit 1; }
    stop
    if command -v crontab >/dev/null 2>&1; then
      (crontab -l 2>/dev/null | grep -v '# HOTS-CONSOLE') | crontab - 2>/dev/null || true
    fi
    if [ -f "${DIR}/htaccess.path" ]; then
      f="$(cat "${DIR}/htaccess.path")"
      [ -f "${f}" ] && sed -i '/# BEGIN HOTS-CONSOLE/,/# END HOTS-CONSOLE/d' "${f}"
      [ -f "${f}" ] && ! grep -q "[^[:space:]]" "${f}" && rm -f "${f}"
    fi
    rm -rf "${DIR}"
    echo "Console supprimée." ;;
  *) sed -n '2,3p' "$0"; exit 1 ;;
esac
EOF
chmod 700 "${DIR}/hots"

# --- 5. Database + start -----------------------------------------------------------------------------------
step "Démarrage"
"${DIR}/hots" stop >/dev/null 2>&1 || true
"${DIR}/hots" start || die "la console n'a pas démarré (journal : ${DIR}/logs/hots.log)."

# --- 6. Crontab: start at boot, watchdog, daily backup ---------------------------------------------------------
if [ "${CRON}" = 1 ]; then
  step "Démarrage automatique (crontab)"
  if command -v crontab >/dev/null 2>&1 && { crontab -l >/dev/null 2>&1 || [ $? = 1 ]; }; then
    m=$(( $(printf '%s' "${DOMAIN}" | cksum | cut -d' ' -f1) % 60 ))
    { crontab -l 2>/dev/null | grep -v '# HOTS-CONSOLE'
      echo "@reboot ${DIR}/hots start >/dev/null 2>&1 # HOTS-CONSOLE"
      echo "*/5 * * * * ${DIR}/hots watchdog >/dev/null 2>&1 # HOTS-CONSOLE"
      echo "${m} 3 * * * ${DIR}/hots backup >/dev/null 2>&1 # HOTS-CONSOLE"
    } | crontab - && say "Tâches planifiées ajoutées (démarrage, surveillance toutes les 5 minutes, sauvegarde quotidienne)." \
      || CRON=2
  else
    CRON=2
  fi
  if [ "${CRON}" = 2 ]; then
    say "La commande crontab n'est pas utilisable. Ajoutez ces tâches dans votre panneau"
    say "(Webmin/Virtualmin : Système → Tâches planifiées (Cron) ; ou « Scheduled Cron Jobs ») :"
    say "  @reboot      ${DIR}/hots start"
    say "  */5 * * * *  ${DIR}/hots watchdog"
    say "  0 3 * * *    ${DIR}/hots backup"
  fi
fi

# --- 7. Web server: Apache .htaccess proxy -------------------------------------------------------------------------
step "Publication sur https://${DOMAIN}"
if [ "${HTACCESS}" = 1 ]; then
  if [ -z "${WEBROOT}" ]; then
    host="${DOMAIN%%:*}"
    for d in "${HOME}/domains/${host}/public_html" "${HOME}/public_html" "${HOME}/www" "${HOME}/httpdocs"; do
      [ -d "${d}" ] && WEBROOT="${d}" && break
    done
    ask WEBROOT "Dossier web du domaine ${host} (laisser vide pour configurer le proxy vous-même)" "${WEBROOT}"
  fi
fi
if [ "${HTACCESS}" = 1 ] && [ -n "${WEBROOT}" ] && [ -d "${WEBROOT}" ]; then
  HT="${WEBROOT}/.htaccess"
  if [ -f "${HT}" ] && [ ! -f "${HT}.avant-hots" ] && ! grep -q '# BEGIN HOTS-CONSOLE' "${HT}"; then
    cp "${HT}" "${HT}.avant-hots"
  fi
  tmp="$(mktemp)"
  { [ -f "${HT}" ] && sed '/# BEGIN HOTS-CONSOLE/,/# END HOTS-CONSOLE/d' "${HT}"
    cat <<EOF
# BEGIN HOTS-CONSOLE (console HOTS Hosts : tout le site est relayé vers le service local)
<IfModule mod_dir.c>
DirectoryIndex disabled
</IfModule>
RewriteEngine On
RewriteCond %{HTTPS} !=on
RewriteCond %{REQUEST_URI} !^/\.well-known/
RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
<IfModule mod_headers.c>
RequestHeader set X-Forwarded-Proto "https"
</IfModule>
RewriteCond %{REQUEST_URI} !^/\.well-known/
RewriteRule ^(.*)$ http://127.0.0.1:${PORT}/\$1 [P,L]
# END HOTS-CONSOLE
EOF
  } > "${tmp}"
  cat "${tmp}" > "${HT}"
  rm -f "${tmp}"
  chmod 644 "${HT}"
  printf '%s\n' "${HT}" > "${DIR}/htaccess.path"
  if [ -f "${HT}.avant-hots" ]; then say "Fichier ${HT} écrit (ancien fichier gardé : .htaccess.avant-hots)."
  else say "Fichier ${HT} écrit."; fi
  code="$("${PY}" - "https://${DOMAIN}/healthz" <<'PYEOF' 2>/dev/null || true
import sys, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=10).status)
except Exception as e:
    print(getattr(e, "code", "") or type(e).__name__)
PYEOF
)"
  if [ "${code}" = "200" ]; then
    say "✅ https://${DOMAIN} répond : la console est en ligne."
  else
    say "⚠️  https://${DOMAIN} ne répond pas encore (${code:-pas de réponse}). Vérifiez :"
    say "   - le certificat HTTPS du domaine (Virtualmin : Paramètres du serveur → Certificat SSL → Let's Encrypt) ;"
    say "   - que le relais Apache est autorisé (modules proxy et proxy_http) ;"
    say "   - sinon, configurez le relais dans le panneau : « / » → http://127.0.0.1:${PORT}/"
    say "   Test local du service : ${DIR}/hots status"
  fi
else
  say "Configurez le relais dans votre panneau pour le domaine ${DOMAIN} :"
  say "   Virtualmin : Configuration Web → Chemins de proxy (Proxy Paths) : « / » → http://127.0.0.1:${PORT}/"
  say "   (selon la version : Website Redirects / Proxy website). Puis activez le certificat Let's Encrypt."
fi

# --- 8. Next step --------------------------------------------------------------------------------------------------------
step "Terminé"
link="$("${DIR}/hots" setup-link)"
say "Ouvrez ce lien pour créer le compte administrateur :"
say ""
say "   ${link}"
say ""
say "Commandes : ${DIR}/hots status | restart | logs | setup-link | reset-password EMAIL | backup | uninstall"
say "Mise à jour : décompressez la nouvelle version et relancez ./install-mutualise.sh (données conservées)."
[ "${WAS_RUNNING}" = 1 ] && say "(Mise à jour effectuée, la console a redémarré.)"
exit 0
