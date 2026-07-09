import { createHash } from 'node:crypto';

export function slugify(value) {
  const slug = String(value ?? '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-');
  return slug || 'untitled';
}

export function stableHash(value, length = 10) {
  return createHash('sha256').update(String(value ?? '')).digest('hex').slice(0, length);
}

export function stableViewName(label, sourceKey, options = {}) {
  const suffixLength = options.suffixLength ?? 8;
  return `${slugify(label)}--${stableHash(sourceKey || label, suffixLength)}`;
}
