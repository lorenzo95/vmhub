#!/usr/bin/env bash
# Stage 2 for a Debian 13 cloud VM: Xfce on Wayland, autologin, Mint-ish theme.
#
# Run it through vmhub from the host:
#
#   vmctl exec myvm -- sudo bash -s < examples/stage2-xfce-wayland.sh
#
# It is deliberately a plain script rather than cloud-init: every step has an
# exit code, the whole thing can be re-run, and a failure is visible instead
# of becoming a line in /var/log/cloud-init-output.log.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root: vmctl exec <vm> -- sudo bash -s < $0" >&2
  exit 1
fi

SESSION_USER=${SESSION_USER:-${SUDO_USER:-debian}}

apt-get update

# The genericcloud image ships Debian's `cloud` kernel, which carries no DRM
# drivers. QEMU hands the guest a virtio-gpu, but without a DRM driver only
# efifb exists: logind reports CanGraphical=no, LightDM never starts a seat,
# and labwc has nothing to render to. Install the generic kernel and remove
# the cloud flavour so GRUB's default entry is unambiguous.
apt-get install -y linux-image-amd64
cloud_pkgs=()
for pkg in linux-image-cloud-amd64 $(dpkg-query -W -f='${Package}\n' 'linux-image-*-cloud-amd64' 2>/dev/null || true); do
  if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
    cloud_pkgs+=("$pkg")
  fi
done
if (( ${#cloud_pkgs[@]} )); then
  apt-get purge -y "${cloud_pkgs[@]}"
fi

# xfce4 is X11 by default. The Wayland session is the 4.20 tech-preview
# path and needs labwc as the compositor; startxfce4 --wayland launches
# `labwc --session xfce4-session`.
apt-get install -y --no-install-recommends \
  xfce4 \
  xfce4-goodies \
  xfce4-whiskermenu-plugin \
  labwc \
  lightdm \
  lightdm-gtk-greeter \
  papirus-icon-theme \
  arc-theme \
  dbus-x11 \
  xdg-user-dirs \
  xdg-utils \
  fonts-dejavu \
  fonts-noto

# Debian ships the Wayland session hidden because it is experimental.
if [[ -f /usr/share/wayland-sessions/xfce-wayland.desktop ]]; then
  sed -i 's/^Hidden=true/#Hidden=true/' /usr/share/wayland-sessions/xfce-wayland.desktop
fi

# Autologin, so the VM comes up at a desktop rather than a greeter.
install -d /etc/lightdm/lightdm.conf.d
cat >/etc/lightdm/lightdm.conf.d/50-vmhub-autologin.conf <<EOF
[Seat:*]
autologin-user=${SESSION_USER}
autologin-user-timeout=0
autologin-session=xfce-wayland
EOF

# System-wide Xfce defaults. Arc + Papirus is the closest Debian gets to
# Mint-Y without importing Mint's Ubuntu-built packages.
install -d /etc/xdg/xfce4/xfconf/xfce-perchannel-xml
cat >/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="Arc-Dark"/>
    <property name="IconThemeName" type="string" value="Papirus-Dark"/>
  </property>
  <property name="Xft" type="empty">
    <property name="DPI" type="int" value="96"/>
    <property name="Antialias" type="int" value="1"/>
    <property name="HintStyle" type="string" value="hintslight"/>
    <property name="RGBA" type="int" value="1"/>
  </property>
</channel>
EOF

systemctl set-default graphical.target
systemctl enable lightdm

echo
echo "Stage 2 done. Reboot, then check the session type:"
echo "  sudo reboot"
echo "  loginctl show-session \$(loginctl list-sessions --no-legend | awk '\$3==\"seat0\"{print \$1}') -p Type"
