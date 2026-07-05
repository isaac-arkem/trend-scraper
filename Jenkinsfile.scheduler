pipeline {
    agent any

    triggers {
        // runs every minute
        cron('* * * * *')
    }

    environment {
        PYTHONUNBUFFERED = '1'
        // job name of the scraper pipeline to trigger
        JENKINS_JOB_NAME = 'trend-scraper'
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

        stage('Run scheduler') {
            steps {
                withCredentials([
                    string(credentialsId: 'supabase-url',        variable: 'SUPABASE_URL'),
                    string(credentialsId: 'supabase-secret-key', variable: 'SUPABASE_SECRET_KEY'),
                    string(credentialsId: 'jenkins-url',         variable: 'JENKINS_URL'),
                    string(credentialsId: 'jenkins-user',        variable: 'JENKINS_USER'),
                    string(credentialsId: 'jenkins-api-token',   variable: 'JENKINS_API_TOKEN'),
                ]) {
                    sh '.venv/bin/python scheduler.py'
                }
            }
        }
    }

    post {
        failure {
            echo "Scheduler tick failed"
        }
    }
}
