export AUTHORIZATION_CODE=4/0AVG7fiS1hktxoUb7_t5rkXEry5Iqrb-H4XCa6s5yma-jzda3qqxE4AJDtzYqtB1wuCR5sg
export CLIENT_ID=1049690543503-eskj0ing349dalshtmjlluargne4vis8.apps.googleusercontent.com
export CLIENT_SECRET=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
curl --request POST --data "code=${AUTHORIZATION_CODE}&client_id=${CLIENT_ID}&client_secret=${CLIENT_SECRET}&redirect_uri=https://github.com/yamigil&grant_type=authorization_code" https://oauth2.googleapis.com/token
