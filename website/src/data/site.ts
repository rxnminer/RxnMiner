export type SiteLocale = 'en';
export type SiteSection = 'home' | 'demos' | 'benchmark';
export const repositoryUrl = 'https://github.com/rxnminer/RxnMiner';
export const navigation = {
  en: {home:'Home', demos:'Demo', benchmark:'Benchmark'},
} as const;
export function assetPath(path: string) {
  return `${import.meta.env.BASE_URL.replace(/\/$/, '')}/${path.replace(/^\//, '')}`;
}
export function localizedPath(_locale: SiteLocale, path: string) { return assetPath(path); }
