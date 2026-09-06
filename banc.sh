#!/usr/bin/env bash
#
# banc.sh — démarre le banc visuo-tactile de bout en bout, depuis le Mac.
#
#   ./banc.sh start      démarre la VM, route le matériel, lance l'interface
#   ./banc.sh stop       arrête l'interface et ferme le tunnel
#   ./banc.sh eteindre   arrête proprement le serveur puis la VM
#   ./banc.sh reset      débloque UTM quand la redirection USB a lâché
#   ./banc.sh status     où en est chaque pièce
#   ./banc.sh logs       suit le journal du serveur
#   ./banc.sh selftest   vérifie le banc, sans acquisition
#   ./banc.sh pose       aide au placement de la caméra, en direct
#   ./banc.sh shell      un shell dans la VM, au bon dossier
#
# Options de « start » :
#   --sans-main          ne pas monter EtherCAT (dispense des droits root)
#   --sans-profondeur    ne diffuser que la couleur (allège le lien USB)
#   --simulation         banc simulé, aucun matériel touché
#   --port N             port de l'interface (défaut 8090)
#
# Le mot de passe sudo de la VM se définit par VT_SUDO_PASS — il n'a pas de
# valeur par défaut, et les commandes qui en ont besoin le réclament.
#
set -uo pipefail

VM="${VT_VM:-Ubuntu24lts}"
HOTE="${VT_HOTE:-openclaw-vm}"
PORT="${VT_PORT:-8090}"
DOSSIER="${VT_DOSSIER:-~/VT-Control}"
# Surtout pas /tmp : Ubuntu le vide au démarrage, et une session d'acquisition
# y disparaîtrait au premier redémarrage de la machine virtuelle — laquelle
# redémarre souvent, la redirection USB s'y bloquant régulièrement.
SESSIONS="${VT_SESSIONS:-~/sessions-vt}"
# Aucune valeur par défaut : un mot de passe en dur dans un dépôt public est
# un secret publié. À définir dans l'environnement, ou dans un fichier non
# versionné que l'on source avant d'appeler ce script.
SUDO_PASS="${VT_SUDO_PASS:-}"
UTMCTL="${VT_UTMCTL:-/Applications/UTM.app/Contents/MacOS/utmctl}"
LOG="/tmp/vtctl-serve.log"
# Le serveur tourne sous sudo, donc le site-packages *utilisateur* — où vivent
# pyrealsense2 et consorts — n'est plus sur le chemin : root a le sien. On le
# résout donc à distance plutôt que de coder en dur un nom d'utilisateur et une
# version de Python.
PYPATH="${VT_PYPATH:-}"

# VID:PID des trois périphériques du banc. La forme VID:PID plutôt que la
# « Location » : celle-ci change à chaque redémarrage d'UTM.
D405="8086:0B5B"        # caméra de profondeur
FEATHER="239A:811B"     # ESP32 : variateur de lumière ET pont infrarouge
ETHERCAT="0BDA:8153"    # adaptateur Realtek : lien EtherCAT vers la main
WEBCAM="046D:082D"      # C920 : source d'angle du plateau, vue plongeante

G=$'\033[32m'; J=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
ok()   { printf "  %s✓%s %s\n" "$G" "$N" "$*"; }
warn() { printf "  %s!%s %s\n" "$J" "$N" "$*"; }
err()  { printf "  %s✗%s %s\n" "$R" "$N" "$*"; }
info() { printf "  %s%s%s\n" "$D" "$*" "$N"; }

vm_lancee() { "$UTMCTL" list 2>/dev/null | grep -qi "started.*$VM"; }
vm_joignable() { ssh -o BatchMode=yes -o ConnectTimeout=4 "$HOTE" true 2>/dev/null; }
sur_vm() { ssh -o BatchMode=yes "$HOTE" "$@"; }
# ``sudo -S -p ""`` : lit le mot de passe sur l'entrée standard et n'écrit
# aucune invite, sinon elle se mélange à la sortie du programme.
# Le mot de passe n'est exigé que par les commandes qui en ont besoin : le
# statut, les journaux et l'arrêt doivent marcher sans lui.
exige_mot_de_passe() {
  [ -n "$SUDO_PASS" ] && return 0
  err "mot de passe sudo de la VM non défini"
  info "l'exporter avant d'appeler ce script :  export VT_SUDO_PASS='…'"
  return 1
}

resoudre_pypath() {
  [ -n "$PYPATH" ] && return 0
  PYPATH=$(sur_vm 'python3 -c "import site,sys;print(site.getusersitepackages())"' 2>/dev/null)
  [ -n "$PYPATH" ] || warn "site-packages utilisateur non résolu sur la VM"
}

sudo_vm() { ssh -o BatchMode=yes "$HOTE" "echo '$SUDO_PASS' | sudo -S -p '' $*"; }

# ── VM ────────────────────────────────────────────────────────────────────────

demarrer_vm() {
  if vm_joignable; then ok "VM déjà joignable"; return 0; fi
  if ! vm_lancee; then
    info "démarrage de la VM $VM…"
    "$UTMCTL" start "$VM" >/dev/null 2>&1
  fi
  printf "  %sattente du démarrage" "$D"
  for _ in $(seq 1 60); do
    if vm_joignable; then printf "%s\n" "$N"; ok "VM joignable"; return 0; fi
    printf "."; sleep 3
  done
  printf "%s\n" "$N"; err "la VM ne répond pas après 3 minutes"; return 1
}

# ── Matériel ──────────────────────────────────────────────────────────────────

router() {
  local id="$1" nom="$2" sortie
  # « already connected » n'est pas un échec : UTM le dit quand le périphérique
  # est déjà attaché à la VM, ce qui est exactement ce qu'on veut.
  sortie=$("$UTMCTL" usb connect "$VM" "$id" 2>&1 | tail -1)
  if [ -z "$sortie" ] || echo "$sortie" | grep -qi "already connected"; then
    ok "$nom routé"
    return 0
  fi
  # Le plan de contrôle d'UTM se fige parfois : une seconde tentative après une
  # pause suffit souvent, sinon on le dit sans bloquer le reste.
  sleep 4
  sortie=$("$UTMCTL" usb connect "$VM" "$id" 2>&1 | tail -1)
  if [ -z "$sortie" ] || echo "$sortie" | grep -qi "already connected"; then
    ok "$nom routé (deuxième tentative)"
    return 0
  fi
  warn "$nom NON routé — $sortie"
  return 1
}

router_materiel() {
  local avec_main="$1" liste
  liste=$("$UTMCTL" usb list 2>&1)

  # « No devices found » alors que le Mac les voit dans ioreg : ce n'est pas le
  # matériel, c'est le plan de contrôle d'UTM qui s'est figé. Le dire tout de
  # suite évite de chercher du côté des câbles.
  if echo "$liste" | grep -qi "No devices found"; then
    if ioreg -p IOUSB -w0 -l 2>/dev/null | grep -q "Depth Camera 405"; then
      err "UTM ne propose aucun périphérique alors que le Mac les voit."
      info "son plan de contrôle USB est figé — lancer « ./banc.sh reset »"
      return 1
    fi
    err "aucun périphérique USB : vérifier le concentrateur"
    return 1
  fi
  echo "$liste" | grep -qi "$D405" || \
    warn "la D405 n'est pas proposée par UTM — vérifier le concentrateur"
  router "$FEATHER" "ESP32 (lampe + plateau)"
  router "$D405"    "caméra D405"
  # La C920 mesure l'angle du plateau. UTM plafonne à quatre périphériques
  # partagés : avec la main on y est pile, il n'y a plus de marge.
  router "$WEBCAM"  "webcam C920 (angle plateau)"
  if [ "$avec_main" = "oui" ]; then
    if router "$ETHERCAT" "adaptateur EtherCAT"; then
      sleep 4
      preparer_ethercat
    fi
  fi
  sleep 3
}

preparer_ethercat() {
  # L'interface arrive DOWN : le maître EtherCAT a besoin qu'elle soit UP, et
  # rien ne le fait à sa place.
  local iface
  iface=$(sur_vm 'ip -br link | awk "/^enx/{print \$1}"' 2>/dev/null | head -1)
  if [ -z "$iface" ]; then warn "aucune interface enx dans la VM"; return 1; fi
  exige_mot_de_passe || return 1
  sudo_vm "ip link set $iface up" >/dev/null 2>&1
  sleep 4
  local carrier vitesse
  carrier=$(sur_vm "cat /sys/class/net/$iface/carrier 2>/dev/null")
  vitesse=$(sur_vm "cat /sys/class/net/$iface/speed 2>/dev/null")
  if [ "$carrier" = "1" ]; then
    ok "EtherCAT $iface — porteuse, ${vitesse:-?} Mb/s"
  else
    warn "EtherCAT $iface sans porteuse — câble vers la main, ou main hors tension"
  fi
}

verifier_demons() {
  # lhandpro_service tient le maître EtherCAT sans poser le moindre verrou.
  # Sans ce contrôle, la connexion échoue trente secondes plus tard sur un
  # message qui ne dit pas pourquoi.
  local d
  d=$(sur_vm 'pgrep -af "lhandpro|py3noca[p]" 2>/dev/null | grep -v pgrep' || true)
  if [ -n "$d" ]; then
    warn "des processus tiennent le bus EtherCAT :"
    echo "$d" | sed 's/^/      /'
    info "c'est le travail de quelqu'un — les arrêter avant de continuer"
    return 1
  fi
  return 0
}

# ── Extinction ────────────────────────────────────────────────────────────────

cmd_eteindre() {
  # L'ordre compte : le serveur d'abord, parce que sa clôture rouvre la main et
  # coupe le couple. La main n'est **pas** rétro-entraînable — l'éteindre en
  # cours de saisie la laisse serrée sur l'objet jusqu'à la prochaine mise sous
  # tension.
  echo
  printf "%sExtinction du banc%s\n\n" "$J" "$N"
  if ! vm_joignable; then ok "VM déjà arrêtée"; echo; return 0; fi

  arreter_serveur
  ok "serveur arrêté — main rouverte, couple coupé"
  pkill -f "ssh -N -L $PORT:" 2>/dev/null && ok "tunnel fermé" || true

  local n
  n=$(sur_vm "ls -d $SESSIONS/*/ 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  [ -n "$n" ] && [ "$n" != "0" ] && info "$n session(s) dans $SESSIONS (conservées)"

  info "arrêt de l'invité…"
  sudo_vm "systemctl poweroff" >/dev/null 2>&1
  for _ in $(seq 1 40); do
    vm_lancee || { ok "VM éteinte"; echo; return 0; }
    sleep 3
  done
  warn "la VM ne s'arrête pas — la fermer depuis UTM"
  echo
}

# ── Remise à zéro d'UTM ───────────────────────────────────────────────────────

cmd_reset() {
  # Le plan de contrôle USB d'UTM se fige, et il se fige souvent. Deux
  # symptômes, une seule cause :
  #
  #   « Cannot connect an already connected usb device » alors que la VM ne voit
  #   rien, et « No devices found » alors que le Mac voit très bien les
  #   périphériques dans ioreg.
  #
  # Rien ne le débloque depuis l'extérieur : il faut arrêter proprement
  # l'invité — pas tuer QEMU, sinon le disque trinque — puis relancer UTM.
  echo
  printf "%sRemise à zéro d'UTM%s\n\n" "$J" "$N"

  if vm_joignable; then
    info "arrêt propre de l'invité…"
    arreter_serveur
    sudo_vm "systemctl poweroff" >/dev/null 2>&1
    for _ in $(seq 1 40); do
      vm_lancee || break
      sleep 3
    done
  fi
  vm_lancee && warn "l'invité ne s'arrête pas — on relance UTM quand même"

  info "arrêt d'UTM…"
  pkill -f "MacOS/UTM" 2>/dev/null
  sleep 5
  pgrep -f "MacOS/UTM" >/dev/null 2>&1 && { pkill -9 -f "MacOS/UTM"; sleep 3; }
  ok "UTM arrêté"

  open -a UTM
  for _ in $(seq 1 30); do
    "$UTMCTL" list >/dev/null 2>&1 && break
    sleep 2
  done
  ok "UTM relancé"

  demarrer_vm || return 1
  ok "prêt — relancer avec ./banc.sh start"
  echo
}

# ── Serveur ───────────────────────────────────────────────────────────────────

arreter_serveur() {
  sudo_vm 'pkill -f "m vtctl"' >/dev/null 2>&1
  sur_vm 'pkill -f "m vtctl"' >/dev/null 2>&1
  sleep 3
}

lancer_serveur() {
  local opts="$1" avec_main="$2" cmd
  # Trois contraintes se contredisent, et c'est ce qui rend ce lancement
  # laborieux :
  #
  # 1. ``setsid`` est indispensable — sans lui le serveur meurt à la fermeture
  #    du canal ssh, même sous ``nohup`` : c'est le groupe de processus que ssh
  #    emporte, pas le signal HUP.
  # 2. ``sudo -S`` veut lire le mot de passe sur l'entrée standard, donc on ne
  #    peut pas la rediriger depuis /dev/null.
  # 3. mettre les identifiants en cache par ``sudo -v`` dans une session ssh
  #    précédente ne sert à rien : ``tty_tickets`` est actif par défaut, et le
  #    cache ne franchit pas la session.
  #
  # La sortie est une chaîne ici-même : sudo consomme la première ligne, et le
  # reste de l'entrée standard va au serveur — qui n'en lit jamais rien.
  if [ "$avec_main" = "oui" ]; then
    exige_mot_de_passe || return 1
    resoudre_pypath
    cmd="setsid sudo -S -p '' env PYTHONPATH=$PYPATH python3 -u -m vtctl $opts --out $SESSIONS serve --port $PORT"
    sur_vm "cd $DOSSIER && ($cmd > $LOG 2>&1 <<< '$SUDO_PASS' &) ; sleep 1" >/dev/null 2>&1
  else
    cmd="setsid python3 -u -m vtctl $opts --out $SESSIONS serve --port $PORT"
    sur_vm "cd $DOSSIER && ($cmd > $LOG 2>&1 < /dev/null &) ; sleep 1" >/dev/null 2>&1
  fi

  printf "  %sattente du serveur" "$D"
  for _ in $(seq 1 45); do
    if sur_vm "curl -s -m 2 http://127.0.0.1:$PORT/api/state >/dev/null 2>&1"; then
      printf "%s\n" "$N"; ok "serveur en écoute sur le port $PORT"; return 0
    fi
    printf "."; sleep 2
  done
  printf "%s\n" "$N"
  err "le serveur n'écoute pas — fin du journal :"
  sur_vm "tail -12 $LOG" 2>/dev/null | grep -vE '^[✅⏳🚀📊🔍🔌]|^  【' | sed 's/^/      /'
  return 1
}

ouvrir_tunnel() {
  pkill -f "ssh -N -L $PORT:" 2>/dev/null
  sleep 1
  ssh -f -N -L "$PORT:127.0.0.1:$PORT" "$HOTE" 2>/dev/null
  sleep 2
  if curl -s -m 4 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
    ok "tunnel ouvert"
    return 0
  fi
  err "le tunnel ne passe pas"
  return 1
}

# ── Commandes ─────────────────────────────────────────────────────────────────

cmd_start() {
  local opts="" avec_main="oui" simulation="non"
  while [ $# -gt 0 ]; do
    case "$1" in
      --sans-main)       opts="$opts --sans-main"; avec_main="non" ;;
      --sans-profondeur) opts="$opts --sans-profondeur" ;;
      --simulation)      opts="$opts --simulation --rapide"; simulation="oui"; avec_main="non" ;;
      --port)            PORT="$2"; shift ;;
      *)                 opts="$opts $1" ;;
    esac
    shift
  done

  echo
  printf "%sBanc visuo-tactile%s\n\n" "$G" "$N"
  demarrer_vm || return 1

  if [ "$simulation" = "non" ]; then
    router_materiel "$avec_main" || return 1
    [ "$avec_main" = "oui" ] && { verifier_demons || return 1; }
  else
    info "mode simulation — aucun matériel touché"
  fi

  arreter_serveur
  lancer_serveur "$opts" "$avec_main" || return 1
  ouvrir_tunnel || return 1

  echo
  printf "  Interface : %shttp://127.0.0.1:%s%s\n" "$G" "$PORT" "$N"
  info "sessions dans $SESSIONS (côté VM)"
  info "journal : ./banc.sh logs      arrêt : ./banc.sh stop"
  echo
  cmd_status
}

cmd_stop() {
  echo
  arreter_serveur
  ok "serveur arrêté"
  pkill -f "ssh -N -L $PORT:" 2>/dev/null && ok "tunnel fermé" || info "pas de tunnel ouvert"
  echo
}

cmd_status() {
  printf "  %sÉtat%s\n" "$D" "$N"
  vm_joignable && ok "VM $VM joignable" || { err "VM injoignable"; return 1; }

  local usb
  usb=$(sur_vm 'lsusb 2>/dev/null' || true)
  echo "$usb" | grep -qi "8086:0b5b" && ok "caméra D405"        || warn "caméra absente"
  echo "$usb" | grep -qi "239a:811b" && ok "ESP32 (ttyACM0)"    || warn "ESP32 absent"
  if echo "$usb" | grep -qi "046d:082d"; then
    local v
    # Par nom, jamais par numéro : /dev/video0 est la D405 dès qu'elle est
    # routée — la RealSense expose six nœuds, la C920 vient après.
    v=$(sur_vm 'grep -l C920 /sys/class/video4linux/video*/name 2>/dev/null | head -1 | sed "s|/sys/class/video4linux/|/dev/|;s|/name||"')
    [ -n "$v" ] && ok "webcam C920 ($v)" \
                || warn "webcam C920 vue mais aucun nœud v4l2 à son nom"
  else
    warn "webcam C920 absente — l'angle retombera sur la D405"
  fi
  if echo "$usb" | grep -qi "0bda:8153"; then
    local iface carrier
    iface=$(sur_vm 'ip -br link | awk "/^enx/{print \$1}"' | head -1)
    carrier=$(sur_vm "cat /sys/class/net/$iface/carrier 2>/dev/null")
    [ "$carrier" = "1" ] && ok "EtherCAT $iface (porteuse)" \
                         || warn "EtherCAT $iface sans porteuse"
  else
    warn "adaptateur EtherCAT absent"
  fi

  if curl -s -m 4 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
    ok "interface accessible sur http://127.0.0.1:$PORT"
  else
    warn "interface non accessible"
  fi
}

cmd_logs()     { sur_vm "tail -f $LOG"; }
cmd_shell()    { ssh -t "$HOTE" "cd $DOSSIER && exec bash -l"; }
cmd_selftest() {
  exige_mot_de_passe || return 1
  resoudre_pypath
  arreter_serveur
  ssh -t "$HOTE" "cd $DOSSIER && echo '$SUDO_PASS' | sudo -S -p '' \
      env PYTHONPATH=$PYPATH python3 -m vtctl --log warning selftest $*"
}
cmd_pose() {
  arreter_serveur
  ssh -t "$HOTE" "cd $DOSSIER && python3 -m vtctl --sans-main --sans-profondeur \
      --log warning pose --suivre ${1:-180}"
}

case "${1:-start}" in
  start)    shift 2>/dev/null; cmd_start "$@" ;;
  reset)    cmd_reset ;;
  eteindre) cmd_eteindre ;;
  stop)     cmd_stop ;;
  status)   echo; cmd_status; echo ;;
  logs)     cmd_logs ;;
  shell)    cmd_shell ;;
  selftest) shift 2>/dev/null; cmd_selftest "$@" ;;
  pose)     shift 2>/dev/null; cmd_pose "$@" ;;
  *)        sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
