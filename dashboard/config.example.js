// Copy to config.js next to index.html (index.html already loads it). config.js is git-ignored.
// On the local Docker stack, scripts/demo-up.sh writes it for you.
// The anon key is public by design; Row Level Security limits reads to signed-in @creai.mx accounts.
window.AXIS_USAGE_CONFIG = {
  supabaseUrl: "https://<project-ref>.supabase.co",
  supabaseAnonKey: "<anon key>",
  // "azure" (Microsoft Entra ID SSO) or "google" once that provider is set up in Supabase Auth;
  // "email" signs in by magic link (the local Docker stack, where no SSO app is registered).
  authProvider: "azure", // ASSUMPTION: creai signs in with Microsoft Entra ID
  // mailViewerUrl: "http://127.0.0.1:54324", // local stack only: where magic-link emails land
};
