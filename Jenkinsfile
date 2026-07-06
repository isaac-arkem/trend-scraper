pipeline {
    agent any

    parameters {
        choice(
            name: 'SCRAPER_TYPE',
            choices: ['dance_trends', 'reference_profiles'],
            description: 'Which scraper to run'
        )
        string(name: 'REQUEST_ID',   defaultValue: '', description: 'DB request row to update with status')
        // dance trends params
        string(name: 'MARKETS',      defaultValue: '', description: 'Comma-separated market codes (e.g. AE,SA,NG)')
        string(name: 'FEEDS',        defaultValue: '', description: 'Comma-separated feed slugs')
        string(name: 'TAGS',         defaultValue: '', description: 'Comma-separated hashtags')
        string(name: 'MIN_VIEWS',    defaultValue: '20000', description: 'Minimum view threshold')
        string(name: 'RECENCY_DAYS', defaultValue: '14',    description: 'How many days back to look')
        // reference profiles params
        string(name: 'HANDLES',      defaultValue: '', description: 'Comma-separated account handles to scrape')
    }

    environment {
        PYTHONUNBUFFERED = '1'
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    if [ ! -d ".venv" ]; then
                        python3 -m venv .venv
                    fi
                    .venv/bin/pip install -q -r requirements.txt
                '''
            }
        }

        stage('Run scraper') {
            steps {
                withCredentials([
                    string(credentialsId: 'supabase-url',        variable: 'SUPABASE_URL'),
                    string(credentialsId: 'supabase-secret-key', variable: 'SUPABASE_SECRET_KEY'),
                    string(credentialsId: 'apify-token',         variable: 'APIFY_TOKEN'),
                    string(credentialsId: 'openai-api-key',      variable: 'OPENAI_API_KEY'),
                    string(credentialsId: 'minio-endpoint',      variable: 'MINIO_ENDPOINT'),
                    string(credentialsId: 'minio-access-key',    variable: 'MINIO_ACCESS_KEY'),
                    string(credentialsId: 'minio-secret-key',    variable: 'MINIO_SECRET_KEY'),
                ]) {
                    script {
                        if (params.SCRAPER_TYPE == 'dance_trends') {
                            withEnv([
                                "REQUEST_ID=${params.REQUEST_ID}",
                                "MARKETS=${params.MARKETS}",
                                "FEEDS=${params.FEEDS}",
                                "TAGS=${params.TAGS}",
                                "MIN_VIEWS=${params.MIN_VIEWS}",
                                "RECENCY_DAYS=${params.RECENCY_DAYS}",
                            ]) {
                                sh '.venv/bin/python scrape_trends.py'
                            }
                        } else if (params.SCRAPER_TYPE == 'reference_profiles') {
                            withEnv([
                                "REQUEST_ID=${params.REQUEST_ID}",
                                "HANDLES=${params.HANDLES}",
                            ]) {
                                sh '''
                                    if [ -n "$HANDLES" ]; then
                                        .venv/bin/python scrape_reference_accounts.py --handles "$HANDLES"
                                    else
                                        .venv/bin/python scrape_reference_accounts.py
                                    fi
                                '''
                            }
                        } else {
                            error("Unknown SCRAPER_TYPE: ${params.SCRAPER_TYPE}")
                        }
                    }
                }
            }
        }
    }

    post {
        failure {
            echo "Scraper job failed: ${params.SCRAPER_TYPE} (REQUEST_ID=${params.REQUEST_ID})"
        }
        success {
            echo "Scraper job completed: ${params.SCRAPER_TYPE} (REQUEST_ID=${params.REQUEST_ID})"
        }
        aborted {
            withCredentials([
                string(credentialsId: 'supabase-url',        variable: 'SUPABASE_URL'),
                string(credentialsId: 'supabase-secret-key', variable: 'SUPABASE_SECRET_KEY'),
            ]) {
                sh '''
                    if [ -n "$REQUEST_ID" ]; then
                        TABLE="dance_scrape_requests"
                        if [ "$SCRAPER_TYPE" = "reference_profiles" ]; then
                            TABLE="reference_scrape_requests"
                        fi
                        curl -s -X PATCH "$SUPABASE_URL/rest/v1/$TABLE?id=eq.$REQUEST_ID" \
                            -H "apikey: $SUPABASE_SECRET_KEY" \
                            -H "Authorization: Bearer $SUPABASE_SECRET_KEY" \
                            -H "Content-Type: application/json" \
                            -d "{\"status\":\"failed\",\"error_message\":\"Aborted by user\"}" || true
                    fi
                '''
            }
        }
    }
}
