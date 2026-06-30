#!/usr/bin/env python3
"""
Seed the reference_accounts table from the curated Influencers PDF.
Topic x region x platform, with the team's style notes. Excludes accounts marked
"not for scraping" (Russian ADHD pre-context) and "can't access".
Run AFTER the migration:  .venv/bin/python seed_reference_accounts.py
"""
from src.db.client import get_db, upsert

# (platform, handle, topic, region, notes)
ACCOUNTS = [
    # ── ADHD / Wellness ──────────────────────────────────────────────────────
    ("tiktok", "_lauriefaulkner_", "adhd_wellness", "misc", "generalist ADHD account"),
    ("tiktok", "notnenasmind", "adhd_wellness", "misc", "good generalist influencer"),
    ("tiktok", "laylaadelinex", "adhd_wellness", "misc", "non-talking account, good to copy"),
    ("tiktok", "jehansaidso", "adhd_wellness", "mideast", "not full ADHD but good post to copy"),
    ("tiktok", "farahfezz", "adhd_wellness", "mideast", ""),
    ("tiktok", "wordsbym_", "adhd_wellness", "mideast", ""),
    ("tiktok", "sanathekid", "adhd_wellness", "mideast", "strong case study for Muslim ADHD account"),
    ("tiktok", "afnan_dahbour", "adhd_wellness", "mideast", "good generalist wellbeing x muslim"),
    ("tiktok", "sara_sellz", "adhd_wellness", "mideast", "copyable ADHD content"),
    ("tiktok", "rahelsplanet", "adhd_wellness", "mideast", "adhd muslim"),
    ("tiktok", "shamihap", "adhd_wellness", "mideast", ""),
    ("tiktok", "soulfultherapistrebecca", "adhd_wellness", "mideast", "ADHD / Islamic psychology"),
    ("tiktok", "call_me_atoussa", "adhd_wellness", "mideast", "ADHD generalist - Iranian"),
    ("tiktok", "tati_mockingbird", "adhd_wellness", "russian", ""),
    ("tiktok", "valeriya_neiro", "adhd_wellness", "russian", "strong case study (probably AI)"),
    ("tiktok", "adhd_dasha", "adhd_wellness", "russian", "good content case"),
    ("tiktok", "mkojicc", "adhd_wellness", "russian", "more of a mental health account"),
    ("tiktok", "saranne_wrap", "adhd_wellness", "latam", ""),
    ("tiktok", "isa_kristen22", "adhd_wellness", "latam", ""),
    ("tiktok", "adhdandlatina", "adhd_wellness", "latam", "generalist account"),
    ("tiktok", "totallytdah", "adhd_wellness", "latam", "best account, regular posting, clear ADHD"),
    ("tiktok", "lamarielisa", "adhd_wellness", "latam", "generalist, variety of topics"),
    ("tiktok", "andrealujan_", "adhd_wellness", "latam", "easy post to copy"),
    ("tiktok", "deanna_melillo", "adhd_wellness", "latam", ""),

    # ── Cooking / Mum ────────────────────────────────────────────────────────
    ("instagram", "sherryhour", "cooking_mum", "mideast", "STRONGEST to copy; 'if you combine' opener, aesthetic"),
    ("tiktok", "arsenioeats", "cooking_mum", "mideast", "change for a girl?"),
    ("instagram", "anushachennaa", "cooking_mum", "mideast", "indian, no tiktok"),
    ("tiktok", "kalejunkie", "cooking_mum", "mideast", ""),
    ("tiktok", "busezeynep", "cooking_mum", "mideast", ""),
    ("tiktok", "surthycooks", "cooking_mum", "mideast", ""),
    ("tiktok", "ruhamasfood", "cooking_mum", "mideast", "grandma style"),
    ("tiktok", "jaymejomassoud", "cooking_mum", "mideast", "lebanon strong candidate"),
    ("tiktok", "cookingwithnes_", "cooking_mum", "mideast", "middle eastern dishes"),
    ("tiktok", "saraaa_el1", "cooking_mum", "mideast", "lebanese food, easy editing, no talking"),
    ("tiktok", "__striga4", "cooking_mum", "russian", "few posts but easy copy"),
    ("tiktok", "karima_gab", "cooking_mum", "russian", ""),
    ("tiktok", "di_licious_me_", "cooking_mum", "russian", ""),
    ("tiktok", "alena..mad", "cooking_mum", "russian", "standout"),
    ("tiktok", "nadezhda_aleksandrovna_m", "cooking_mum", "russian", "cliche grandma, garden+food (ai?)"),
    ("tiktok", "evgenia__afanasova", "cooking_mum", "russian", ""),
    ("instagram", "shidaeva_heda", "cooking_mum", "russian", "russian x muslim, no tiktok"),
    ("instagram", "aleksandrinakim", "cooking_mum", "russian", ""),
    ("instagram", "almagul.2909", "cooking_mum", "russian", "eastern russian"),
    ("instagram", "ararooka", "cooking_mum", "russian", "eastern russian; lofi hiphop signature style"),
    ("tiktok", "knarik.karapetyan", "cooking_mum", "russian", "bigger on IG (knarikkarapetian)"),
    ("tiktok", "tania.hlamova", "cooking_mum", "russian", "russian grandmother cooking"),
    ("instagram", "lourfit_", "cooking_mum", "latam", ""),
    ("instagram", "cafecitoconlecheof", "cooking_mum", "latam", ""),
    ("instagram", "cooking_con_omi", "cooking_mum", "latam", ""),
    ("tiktok", "hennashareee", "cooking_mum", "latam", ""),
    ("tiktok", "monagirald0", "cooking_mum", "latam", "voiceover heavy"),
    ("tiktok", "cboothang", "cooking_mum", "latam", ""),
    ("tiktok", "madremilly", "cooking_mum", "latam", "latina grandmother"),
    ("tiktok", "anasofiafehn", "cooking_mum", "latam", "fun, handheld"),
    ("tiktok", "elantojitocolombiano", "cooking_mum", "latam", "colombian"),
    ("tiktok", "anatovarnelson", "cooking_mum", "latam", "mexican"),
    ("tiktok", "patty.plates", "cooking_mum", "misc", "east asian, easy to copy"),
    ("tiktok", "serenagwolf", "cooking_mum", "misc", ""),
    ("tiktok", "momma.jackie65", "cooking_mum", "misc", "classic grandma"),
    ("tiktok", "4bettyg23", "cooking_mum", "misc", ""),
    ("tiktok", "themoniqueelyousf", "cooking_mum", "misc", ""),

    # ── Betting -> Trading / Crypto ──────────────────────────────────────────
    ("tiktok", "youngbullinvestors", "trading", "mideast", ""),
    ("tiktok", "realhannahchan", "trading", "mideast", ""),
    ("tiktok", "malaikahraja", "trading", "mideast", ""),
    ("tiktok", "theonlycrisy", "trading", "mideast", "easy copy"),
    ("tiktok", "genuinelygenesis", "trading", "latam", ""),
    ("tiktok", "tori.trades", "trading", "misc", ""),
    ("tiktok", "rewirewithmilli", "trading", "misc", ""),
    ("tiktok", "sarsland", "trading", "misc", ""),
    ("tiktok", "kyler.pokerwiz", "trading", "misc", "poker"),
    ("tiktok", "wolfessofwallstreet_", "trading", "misc", ""),

    # ── Spirituality / Enlightenment ─────────────────────────────────────────
    ("tiktok", "dailyspark01", "spirituality", "mideast", ""),
    ("tiktok", "khadija.n.osman", "spirituality", "mideast", ""),
    ("tiktok", "danielambiah", "spirituality", "mideast", "good one to copy"),
    ("tiktok", "saraelham", "spirituality", "mideast", ""),
    ("tiktok", "mashkutovaa", "spirituality", "russian", "affirmations (needs translation check)"),
    ("tiktok", "alinakostil111", "spirituality", "russian", ""),
    ("tiktok", "bigsiscassie", "spirituality", "russian", ""),
    ("tiktok", "its_suvorova", "spirituality", "russian", "affirmations, easy copy"),
    ("tiktok", "stephaniemartinezjames", "spirituality", "latam", ""),
    ("tiktok", "beforetheborder", "spirituality", "latam", "AI"),
    ("tiktok", "algorithmdaddy", "spirituality", "latam", "good one to copy"),
    ("tiktok", "motherknows_", "spirituality", "latam", ""),
    ("tiktok", "the.vera.era", "spirituality", "misc", ""),
    ("tiktok", "fit_by_ekaterina", "spirituality", "misc", ""),
    ("tiktok", "erinlyonsofficial", "spirituality", "misc", ""),
    ("tiktok", "kawanisellison", "spirituality", "misc", ""),
    ("tiktok", "lanaviish", "spirituality", "misc", ""),
    ("tiktok", "aprimordialwitch", "spirituality", "misc", "AI spiritual"),
    ("tiktok", "kierstenaniya", "spirituality", "misc", ""),
    ("tiktok", "malinsaraandersson", "spirituality", "misc", ""),
    ("tiktok", "aubreycorrigan", "spirituality", "misc", "catholic girly"),
]


def main():
    rows = [{"platform": p, "handle": h.lower(), "topic": t, "region": r, "notes": n}
            for (p, h, t, r, n) in ACCOUNTS]
    for i in range(0, len(rows), 100):
        upsert("reference_accounts", rows[i:i+100], on_conflict="platform,handle")
    from collections import Counter
    print(f"seeded {len(rows)} reference accounts")
    print("by topic:", dict(Counter(r["topic"] for r in rows)))
    print("by region:", dict(Counter(r["region"] for r in rows)))
    print("by platform:", dict(Counter(r["platform"] for r in rows)))


if __name__ == "__main__":
    main()
