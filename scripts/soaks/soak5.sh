cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1
exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 900 --max-wall-seconds 3600 -v
