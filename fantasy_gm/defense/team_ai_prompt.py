TEAM_DEFENSE_RESEARCH_PROMPT = '''
Research the current fantasy-football context for {team_name} D/ST.

Assess only likely football/fantasy consequences. Do not treat allegations as proof.
A report can matter because it changes availability, preparation, distraction,
coaching, travel, facilities, or game conditions even when no allegation has been proven.

Look for:
- injuries / suspensions / holdouts involving important defenders
- depth-chart promotions/demotions
- coordinator/scheme changes
- credible legal or disciplinary stories affecting availability/preparation
- facility, infrastructure, electrical/power, travel, stadium, or operational events
  that could plausibly affect preparation or game conditions
- clusters of issues affecting multiple defenders
- opponent QB/offensive-line circumstances relevant to early-season D/ST value

Ignore gossip with no plausible football consequence.

Return:
- category
- bounded delta from -5.0 to +4.0
- confidence 0..1
- concise summary
- evidence
'''
