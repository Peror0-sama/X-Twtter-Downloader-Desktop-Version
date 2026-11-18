// 原生 Request/URL/Blob 配合隔离的 fetch/XHR，验证观察器不改变网页请求。
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = JSON.parse(fs.readFileSync(0, 'utf8'));
const endpoint = 'https://x.com/i/api/graphql/query/TweetDetail';
const variables = {focalTweetId: '123456789'};
const query = id => endpoint + '?variables=' + encodeURIComponent(JSON.stringify({focalTweetId: id}));
const tick = () => new Promise(setImmediate);
let responseBody = '{"data":{"test":true}}', lastArgs, nativePromise, fail = false;
const response = {status: 200, clone() {const raw = responseBody; return {text: async () => raw};}};
class XHR {
    constructor() {this.listeners = [];}
    open(method, url) {this.method = method; this.url = url;}
    addEventListener(event, cb, options) {this.listeners.push({event, cb, options});}
    send(body) {this.sent = body; this.complete(200);}
    complete(status) {
        this.status = status; this.responseType = ''; this.responseText = responseBody;
        const listeners = this.listeners; this.listeners = [];
        for (const l of listeners) if (l.event === 'loadend') l.cb();
    }
}
const context = {URL, URLSearchParams, Request, Blob, FormData, WeakMap, JSON, String,
    location: {hostname: 'x.com', href: 'https://x.com/alice/status/123456789'},
    XMLHttpRequest: XHR,
    fetch(...args) {
        lastArgs = args;
        nativePromise = fail ? Promise.reject(new Error('network fixture')) : Promise.resolve(response);
        return nativePromise;
    }};
context.window = context;
vm.createContext(context); vm.runInContext(script, context);
async function fetchCase(input, init, expected) {
    const result = context.fetch(input, init);
    assert.equal(result, nativePromise);
    assert.equal(await result, response);
    await tick(); await tick();
    assert.equal(lastArgs[0], input); assert.equal(lastArgs[1], init);
    assert.equal(context.__xdResponses.length, expected);
    context.__xdResponses.splice(0);
}
async function run() {
    switch (process.argv[2]) {
    case 'get':
        await fetchCase(query('123456789'), undefined, 1);
        await fetchCase(new URL(query('123456789')), undefined, 1);
        await fetchCase(query('other'), undefined, 0);
        break;
    case 'post':
        await fetchCase(endpoint, {method: 'POST', body: JSON.stringify({variables})}, 1);
        await fetchCase(endpoint, {method: 'POST', body: JSON.stringify({variables: JSON.stringify(variables)})}, 1);
        await fetchCase(endpoint, {method: 'POST', body: new URLSearchParams({variables: JSON.stringify(variables)})}, 1);
        await fetchCase(endpoint, {method: 'POST', body: new URLSearchParams({variables: JSON.stringify(variables)}).toString()}, 1);
        await fetchCase(endpoint, {method: 'POST', body: new Blob([JSON.stringify({variables})])}, 1);
        break;
    case 'request': {
        const body = JSON.stringify({variables});
        const request = new Request(endpoint, {method: 'POST', body});
        await fetchCase(request, undefined, 1);
        assert.equal(request.bodyUsed, false); assert.equal(await request.text(), body);
        const overridden = new Request(endpoint, {method: 'POST', body: JSON.stringify({variables: {tweetId: 'other'}})});
        await fetchCase(overridden, {body}, 1);
        assert.equal(overridden.bodyUsed, false);
        break;
    }
    case 'xhr': {
        const x = new context.XMLHttpRequest();
        for (const [url, body, expected] of [[endpoint, JSON.stringify({variables}), 1],
            [query('123456789'), null, 1], [endpoint, JSON.stringify({variables: {tweetId: 'other'}}), 0]]) {
            x.open('POST', url); x.send(body); await tick();
            assert.equal(x.sent, body); assert.equal(context.__xdResponses.length, expected);
            context.__xdResponses.splice(0); assert.equal(x.listeners.length, 0);
        }
        break;
    }
    case 'isolation':
        for (const url of ['https://example.com/i/api/graphql/query', 'https://x.com/unrelated', query('other')])
            await fetchCase(url, {method: 'POST', body: JSON.stringify({variables})}, 0);
        await fetchCase(endpoint, {method: 'POST', body: 'not json'}, 0);
        await fetchCase(endpoint, {method: 'POST', body: JSON.stringify({variables, pad: 'x'.repeat(65536)})}, 0);
        fail = true; await assert.rejects(context.fetch(query('123456789')), /network fixture/); await tick();
        assert.equal(context.__xdResponses.length, 0);
        break;
    case 'responses':
        responseBody = 'not json'; await fetchCase(query('123456789'), undefined, 0);
        responseBody = 'x'.repeat(8388609); await fetchCase(query('123456789'), undefined, 0);
        responseBody = '{}';
        for (let i = 0; i < 8; i++) await context.fetch(query('123456789'));
        await tick(); assert.equal(context.__xdResponses.length, 4);
        break;
    default: throw new Error('Unknown fixture case');
    }
    console.log('PASS ' + process.argv[2]);
}
run().catch(error => {console.error(error); process.exitCode = 1;});
