import sqlite3, json, collections, re

db = sqlite3.connect('/data/readiness.db')
db.row_factory = sqlite3.Row

# feature_details IS the real feature list for 26C (965 rows)
rows = db.execute("""
    SELECT feature_name, module, release, product_family,
           steps_to_enable, tips_considerations, optional_uptake,
           description_full, business_benefit, access_requirements,
           key_resources, other_sections, feature_page_url
    FROM feature_details
    WHERE UPPER(release) = '26C'
    ORDER BY product_family, module, feature_name
""").fetchall()

total = len(rows)

# ── Headline stats ────────────────────────────────────────────────────────────
stats = {
    'total':           total,
    'by_pillar':       dict(collections.Counter(r['product_family'].upper() for r in rows)),
    'with_steps':      sum(1 for r in rows if r['steps_to_enable']),
    'with_tips':       sum(1 for r in rows if r['tips_considerations']),
    'optional_uptake': sum(1 for r in rows if r['optional_uptake']),
    'with_description':sum(1 for r in rows if r['description_full']),
}
print('=STATS=')
print(json.dumps(stats, indent=2))

# ── Theme clusters (feature name keywords) ────────────────────────────────────
themes = {
    'Redwood UX Refresh':    r'\bredwood\b',
    'AI / Agentic Workflows':r'\b(agentic|workflow agent|ai agent|generative ai|ai assist|ai-assist|copilot)\b',
    'Workflow / Approvals':  r'\b(workflow|approval|approver)\b',
    'Reporting / Analytics': r'\b(report|analytics|subject area|dashboard|insight|otbi)\b',
    'Security / Access':     r'\b(security|access|role|privilege|permission|audit)\b',
    'Notifications / Alerts':r'\b(notif|alert|reminder)\b',
    'Search / Filter / Sort':r'\b(search|filter|sort|lookup|keyword)\b',
    'Extensibility / Config':r'\b(extensib|vb studio|configure|personaliz|flexfield|dff|sandbox)\b',
    'Integration / API':     r'\b(integrat|api|rest|export|import|inbound|outbound|oci)\b',
    'Mobile / Accessibility':r'\b(mobile|responsive|accessibility)\b',
    'Performance / Usability':r'\b(performance|usabilit|enhance|improvement|faster|optimiz)\b',
    'Date / Calendar UX':    r'\b(date picker|calendar|time picker|date range)\b',
}

theme_data = collections.defaultdict(list)
for r in rows:
    name_lc = r['feature_name'].lower()
    for theme, pattern in themes.items():
        if re.search(pattern, name_lc, re.I):
            theme_data[theme].append({
                'pillar':  r['product_family'].upper(),
                'module':  r['module'],
                'feature': r['feature_name'],
            })

print('\n\n=THEMES=')
for theme, items in sorted(theme_data.items(), key=lambda x: -len(x[1])):
    pillars = sorted(set(i['pillar'] for i in items))
    mods    = sorted(set(i['module'] for i in items))
    print(f'\n{theme}: {len(items)} features | {len(pillars)} pillar(s): {pillars} | {len(mods)} module(s)')
    for i in sorted(items, key=lambda x: (x['pillar'], x['module'], x['feature'])):
        print(f'    [{i["pillar"]}] {i["module"]:45s}  {i["feature"]}')

# ── Cross-pillar same-concept feature names ───────────────────────────────────
name_map = collections.defaultdict(list)
for r in rows:
    key = r['feature_name'].lower().strip()
    key = re.sub(r'^redwood\s+', '', key)
    key = re.sub(r'[^a-z0-9 ]', ' ', key)
    key = re.sub(r'\s+', ' ', key).strip()
    name_map[key].append({'pillar': r['product_family'].upper(), 'module': r['module'], 'feature': r['feature_name']})

cross = {k: v for k, v in name_map.items() if len(set(i['pillar'] for i in v)) > 1}
print(f'\n\n=CROSS-PILLAR SAME-CONCEPT FEATURES: {len(cross)}=')
for k, v in sorted(cross.items(), key=lambda x: -len(set(i['pillar'] for i in x[1]))):
    print(f'\n  "{k}"')
    for i in sorted(v, key=lambda x: x['pillar']):
        print(f'    {i["pillar"]:8s} / {i["module"]:45s} :: {i["feature"]}')

# ── Steps-to-enable pattern: modules that have steps in MULTIPLE pillars ──────
steps_by_module = collections.defaultdict(list)
for r in rows:
    if r['steps_to_enable']:
        key = r['module'].lower().strip()
        steps_by_module[key].append(r['product_family'].upper())

cross_steps = {k: list(set(v)) for k, v in steps_by_module.items() if len(set(v)) > 1}
print(f'\n\n=MODULES WITH STEPS TO ENABLE ACROSS MULTIPLE PILLARS: {len(cross_steps)}=')
for mod, pillars in sorted(cross_steps.items(), key=lambda x: -len(x[1])):
    print(f'  {mod}: {sorted(pillars)}')

# ── Module frequency per pillar (top modules by feature count) ────────────────
print('\n\n=MODULE FEATURE COUNTS BY PILLAR=')
mod_counts = collections.defaultdict(lambda: collections.defaultdict(int))
for r in rows:
    mod_counts[r['product_family'].upper()][r['module']] += 1

for pillar in sorted(mod_counts):
    print(f'\n{pillar}:')
    for mod, cnt in sorted(mod_counts[pillar].items(), key=lambda x: -x[1]):
        steps = sum(1 for r in rows if r['product_family'].upper()==pillar
                    and r['module']==mod and r['steps_to_enable'])
        tips  = sum(1 for r in rows if r['product_family'].upper()==pillar
                    and r['module']==mod and r['tips_considerations'])
        opt   = sum(1 for r in rows if r['product_family'].upper()==pillar
                    and r['module']==mod and r['optional_uptake'])
        print(f'  {cnt:3d} features  {steps:3d} steps  {tips:3d} tips  {opt:3d} opt  {mod}')
