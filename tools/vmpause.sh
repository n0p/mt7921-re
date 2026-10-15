#!/bin/sh
# Freeze the test VM for <seconds> (fractions allowed) through the QEMU monitor (tmux session vm23 on the lab host):
# every emulated device stalls, USB included, as a host hiccup would. Usage: vmpause.sh <seconds>
H=$(dirname "$0")/h.sh
$H "sudo -n tmux send-keys -t vm23 C-a c; sleep 0.3; sudo -n tmux send-keys -t vm23 stop Enter; sleep $1; sudo -n tmux send-keys -t vm23 cont Enter; sleep 0.3; sudo -n tmux send-keys -t vm23 C-a c"
