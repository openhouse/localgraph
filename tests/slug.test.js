import test from 'node:test';
import assert from 'node:assert/strict';
import { slugify, stableHash, stableViewName } from '../src/slug.js';
import { viewPath } from '../src/views.js';

test('slugify produces filesystem-safe names', () => {
  assert.equal(slugify('Alice Example'), 'alice-example');
  assert.equal(slugify('Café mañana & 東京'), 'cafe-manana-and');
  assert.equal(slugify(''), 'untitled');
});

test('stableViewName combines a readable label with a stable suffix', () => {
  assert.equal(stableViewName('Alice Example', 'instagram:thread:123'), stableViewName('Alice Example', 'instagram:thread:123'));
  assert.notEqual(stableViewName('Alice Example', 'instagram:thread:123'), stableViewName('Alice Example', 'instagram:thread:456'));
});

test('stableHash defaults to ten hex characters', () => {
  assert.match(stableHash('source-key'), /^[a-f0-9]{10}$/);
});

test('viewPath maps supported view kinds into generated view directories', () => {
  const result = viewPath('/archive/localgraph', 'person', 'Alice Example', 'instagram:alice');
  assert.match(result, /\/archive\/localgraph\/views\/people\/alice-example--[a-f0-9]{8}$/);
});

test('viewPath rejects unsupported view kinds', () => {
  assert.throws(() => viewPath('/archive/localgraph', 'account', 'Alice', 'alice'), /Unsupported view kind/);
});
