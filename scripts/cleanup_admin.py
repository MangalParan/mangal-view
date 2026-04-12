import sys
import os

filepath = os.path.join(os.path.dirname(__file__), 'nifty_chart.py')
with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find ADMIN_KEY line and the two @app.route("/admin") lines
admin_key_idx = None
route_indices = []
for i, l in enumerate(lines):
    if 'ADMIN_KEY = os.environ' in l:
        admin_key_idx = i
    if '@app.route("/admin"' in l or "@app.route('/admin'" in l:
        route_indices.append(i)

# Keep: lines 0..admin_key_idx+1 (ADMIN_KEY + blank line), then from second route onward
if admin_key_idx is not None and len(route_indices) >= 2:
    keep_end = admin_key_idx + 2  # include ADMIN_KEY line + one blank line
    new_lines = lines[:keep_end] + ['\n'] + lines[route_indices[1]:]
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    with open(os.path.join(os.path.dirname(__file__), 'cleanup_done.txt'), 'w') as f:
        f.write(f'Done. Removed {len(lines)-len(new_lines)} lines. Old={len(lines)}, New={len(new_lines)}\n')
        f.write(f'admin_key_idx={admin_key_idx}, routes={route_indices}\n')
else:
    with open(os.path.join(os.path.dirname(__file__), 'cleanup_done.txt'), 'w') as f:
        f.write(f'FAILED. admin_key_idx={admin_key_idx}, routes={route_indices}\n')

