'use strict';
function official(value) {
  try { const u = new URL(value); return u.protocol === 'https:' && u.hostname === 'x.com' && !u.port && !u.username && !u.password; }
  catch { return false; }
}
function matches(value, target) {
  try {
    let v = typeof value === 'string' ? JSON.parse(value) : value;
    if (v && v.variables !== undefined) v = typeof v.variables === 'string' ? JSON.parse(v.variables) : v.variables;
    return !!v && ['focalTweetId','tweetId','tweet_id'].some(k => String(v[k] || '') === target);
  } catch { return false; }
}
function relevant(url, method, body, target) {
  if (!official(url)) return false;
  const u = new URL(url);
  if (!u.pathname.includes('/graphql/')) return false;
  if (u.searchParams.has('variables')) return matches(u.searchParams.get('variables'), target);
  if (method !== 'POST' || typeof body !== 'string' || body.length > 65536) return false;
  return matches(body, target) || matches(new URLSearchParams(body).get('variables'), target);
}
module.exports = {official, matches, relevant};
