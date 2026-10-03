const {execFileSync} = require('node:child_process');
const path = require('node:path');
const userAgents = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) Version/15.6 Mobile/15E148',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Mobile/15E148',
    'Mozilla/5.0 (iPad; CPU OS 15_7 like Mac OS X) Mobile/15E148',
    'Mozilla/5.0 (Linux; Android 15) Chrome/130.0',
];
for (const userAgent of userAgents) {
    execFileSync(process.execPath, [path.join(__dirname, 'test_home_hero_native.cjs')], {
        cwd: path.join(__dirname, '..'), env: {...process.env, MD_TEST_USER_AGENT: userAgent},
    });
}
console.log('Old/new iPhones, iPad and Android pass the same native carousel initialization and repeated-swipe checks');
