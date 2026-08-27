// Social Listening scrapers.
//
// One job, four pipelines. The console collects the fields, creates a request
// row, then triggers this job with the matching parameters. The script writes
// its status back to that row, so the UI polls the database rather than tailing
// a build log.
//
//   SCRAPER_TYPE          request table                script
//   reference_profiles    reference_scrape_requests    scrape_reference_accounts.py
//   creator_intelligence  runs                         main.py
//   hashtag_profiles      reference_scrape_requests    scrape_hashtag_profiles.py
//   weekly_trends         dance_scrape_requests        scrape_weekly_trends.py
//
// Every parameter below maps to exactly one field in the console. A parameter
// left empty is not passed to the script at all, so the script's own default
// applies — that is why they are all optional strings rather than typed inputs.

pipeline {
    agent any

    parameters {
        choice(
            name: 'SCRAPER_TYPE',
            choices: ['reference_profiles', 'creator_intelligence', 'hashtag_profiles', 'weekly_trends'],
            description: 'Which pipeline to run'
        )
        string(name: 'REQUEST_ID', defaultValue: '',
               description: 'Row in the matching request table to update with status')
        string(name: 'TITLE', defaultValue: '',
               description: 'Run title, as typed in the console')

        // ── reference_profiles ────────────────────────────────────────────
        string(name: 'ACCOUNTS', defaultValue: '',
               description: 'account=niche pairs, comma separated. Full profile URLs or @handles. Platform is read off each URL.')
        string(name: 'HANDLES', defaultValue: '',
               description: 'Existing handles to refresh, comma separated. Leave blank to scrape every active account.')
        string(name: 'REGION', defaultValue: '',
               description: 'Region tag for newly added accounts')
        string(name: 'POSTS_PER_ACCOUNT', defaultValue: '',
               description: 'Posts to pull per account')

        // ── creator_intelligence ──────────────────────────────────────────
        string(name: 'MARKET', defaultValue: '',
               description: 'Single market code, must exist in the markets table')
        string(name: 'POSTS_PER_CREATOR', defaultValue: '',
               description: 'Posts to pull per creator (also used as posts-per-profile for hashtag_profiles)')
        string(name: 'MAX_CREATORS', defaultValue: '',
               description: 'Cap discovery / enrichment / harvest (also max-profiles for hashtag_profiles)')
        booleanParam(name: 'SKIP_ANALYSIS', defaultValue: false,
               description: 'Skip AI vision. Turn on to avoid OpenAI spend entirely.')

        // ── hashtag_profiles ──────────────────────────────────────────────
        string(name: 'COUNTRIES', defaultValue: '',
               description: 'Comma-separated ISO codes for hashtag profile discovery')
        string(name: 'NICHE', defaultValue: '',
               description: 'Niche assigned to every profile found by hashtag discovery')

        // ── weekly_trends ─────────────────────────────────────────────────
        string(name: 'MARKETS', defaultValue: '',
               description: 'Comma-separated market codes')
        string(name: 'POSTS_PER_TAG', defaultValue: '',
               description: 'Posts to pull per hashtag')
        string(name: 'BOARD_DAYS', defaultValue: '7',
               description: 'Board window. 7 for the weekly board, 30 for month end.')

        // ── shared ────────────────────────────────────────────────────────
        string(name: 'PLATFORM', defaultValue: '',
               description: 'tiktok | instagram | both. Blank means both.')
        string(name: 'TAGS', defaultValue: '',
               description: 'Comma-separated hashtags. Blank uses the trending board or the market defaults.')
        string(name: 'RECENCY_DAYS', defaultValue: '',
               description: 'Only posts from the last N days. Blank means no limit.')
        string(name: 'MIN_VIEWS', defaultValue: '',
               description: 'Ignore posts below this view count')
    }

    environment {
        PYTHONUNBUFFERED = '1'
        TRIAGE_OBJECT_STORE = 'wasabi'
    }

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '50'))
        timeout(time: 8, unit: 'HOURS')     // a full 19-country trends run is ~3.5h
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    set -eu
                    if [ ! -d ".venv" ]; then
                        python3 -m venv .venv
                    fi
                    .venv/bin/pip install -q -r requirements.txt
                '''
            }
        }

        stage('Run') {
            steps {
                withCredentials([
                    string(credentialsId: 'supabase-url',        variable: 'SUPABASE_URL'),
                    string(credentialsId: 'supabase-secret-key', variable: 'SUPABASE_SECRET_KEY'),
                    string(credentialsId: 'apify-token',         variable: 'APIFY_TOKEN'),
                    string(credentialsId: 'openai-api-key',      variable: 'OPENAI_API_KEY'),
                    string(credentialsId: 'wasabi-endpoint',   variable: 'WASABI_ENDPOINT_URL'),
                    string(credentialsId: 'wasabi-access-key', variable: 'WASABI_ACCESS_KEY'),
                    string(credentialsId: 'wasabi-secret-key', variable: 'WASABI_SECRET_KEY'),
                ]) {
                    script {
                        // Build the argument list from whatever was supplied. An
                        // empty parameter is omitted rather than passed as "",
                        // which would override the script's own default with a
                        // blank and silently change behaviour.
                        def arg = { flag, value ->
                            (value?.trim()) ? " ${flag} '${value.trim().replace("'", "'\\''")}'" : ""
                        }

                        def cmd
                        if (params.SCRAPER_TYPE == 'reference_profiles') {
                            cmd = ".venv/bin/python scrape_reference_accounts.py"
                            cmd += arg('--title',              params.TITLE)
                            cmd += arg('--add',                params.ACCOUNTS)
                            cmd += arg('--handles',            params.HANDLES)
                            cmd += arg('--region',             params.REGION)
                            cmd += arg('--platform',           params.PLATFORM)
                            cmd += arg('--posts-per-account',  params.POSTS_PER_ACCOUNT)
                            cmd += arg('--recency-days',       params.RECENCY_DAYS)

                        } else if (params.SCRAPER_TYPE == 'creator_intelligence') {
                            if (!params.MARKET?.trim()) {
                                error("creator_intelligence needs MARKET")
                            }
                            cmd = ".venv/bin/python main.py"
                            cmd += arg('--title',    params.TITLE)
                            cmd += arg('--market',   params.MARKET)
                            cmd += arg('--platform', params.PLATFORM ?: 'both')
                            cmd += arg('--posts',    params.POSTS_PER_CREATOR)
                            cmd += arg('--limit',    params.MAX_CREATORS)
                            cmd += arg('--hashtags', params.TAGS)
                            if (params.SKIP_ANALYSIS) { cmd += " --skip-analysis" }

                        } else if (params.SCRAPER_TYPE == 'hashtag_profiles') {
                            if (!params.COUNTRIES?.trim()) {
                                error("hashtag_profiles needs COUNTRIES")
                            }
                            if (!params.NICHE?.trim()) {
                                error("hashtag_profiles needs NICHE")
                            }
                            if (!params.TAGS?.trim()) {
                                error("hashtag_profiles needs TAGS")
                            }
                            cmd = ".venv/bin/python scrape_hashtag_profiles.py"
                            cmd += arg('--title',             params.TITLE)
                            cmd += arg('--countries',         params.COUNTRIES)
                            cmd += arg('--niche',             params.NICHE)
                            cmd += arg('--hashtags',          params.TAGS)
                            cmd += arg('--posts-per-profile', params.POSTS_PER_CREATOR)
                            cmd += arg('--max-profiles',      params.MAX_CREATORS)
                            cmd += arg('--recency-days',      params.RECENCY_DAYS)
                            cmd += arg('--min-views',         params.MIN_VIEWS)
                            cmd += arg('--platform',          params.PLATFORM ?: 'both')
                            // Console "AI off" path — skip the vision pass.
                            cmd += " --skip-appearance"

                        } else if (params.SCRAPER_TYPE == 'weekly_trends') {
                            if (!params.MARKETS?.trim()) {
                                error("weekly_trends needs MARKETS")
                            }
                            cmd = ".venv/bin/python scrape_weekly_trends.py"
                            cmd += arg('--title',          params.TITLE)
                            cmd += arg('--markets',        params.MARKETS)
                            cmd += arg('--tags',           params.TAGS)
                            cmd += arg('--posts-per-tag',  params.POSTS_PER_TAG)
                            cmd += arg('--recency-days',   params.RECENCY_DAYS)
                            cmd += arg('--min-views',      params.MIN_VIEWS)
                            cmd += arg('--board-days',     params.BOARD_DAYS)

                        } else {
                            error("Unknown SCRAPER_TYPE: ${params.SCRAPER_TYPE}")
                        }

                        echo "→ ${cmd}"
                        withEnv(["REQUEST_ID=${params.REQUEST_ID}"]) {
                            sh cmd
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            script {
                def mins = (currentBuild.duration ?: 0) / 60000
                echo String.format("%s finished in %.1f min — %s",
                                   params.SCRAPER_TYPE, mins, currentBuild.currentResult)
            }
        }
        failure {
            echo "FAILED: ${params.SCRAPER_TYPE} (REQUEST_ID=${params.REQUEST_ID})"
        }
        aborted {
            // The script marks its own row on success and failure, but an abort
            // kills it before it can, leaving the row stuck on 'running' forever.
            withCredentials([
                string(credentialsId: 'supabase-url',        variable: 'SUPABASE_URL'),
                string(credentialsId: 'supabase-secret-key', variable: 'SUPABASE_SECRET_KEY'),
            ]) {
                sh '''
                    set +e
                    if [ -n "''' + "${params.REQUEST_ID}" + '''" ]; then
                      SCRAPER_TYPE=''' + "${params.SCRAPER_TYPE}" + ''' REQUEST_ID=''' + "${params.REQUEST_ID}" + ''' .venv/bin/python - <<'EOF'
import os
from supabase import create_client
tbl = {"reference_profiles": "reference_scrape_requests",
       "hashtag_profiles": "reference_scrape_requests",
       "weekly_trends": "dance_scrape_requests"}.get(os.environ.get("SCRAPER_TYPE", ""))
rid = os.environ.get("REQUEST_ID")
if tbl and rid:
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    db.table(tbl).update({"status": "failed",
                          "error_message": "aborted in Jenkins"}).eq("id", rid).execute()
EOF
                    fi
                '''
            }
        }
    }
}
