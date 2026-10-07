// Copy to config.js next to index.html (index.html already loads it). config.js is git-ignored.
// The anon key is public by design; Row Level Security limits reads to signed-in @creai.mx accounts.
window.AXIS_USAGE_CONFIG = {
  supabaseUrl: "https://<project-ref>.supabase.co",
  supabaseAnonKey: "<anon key>",
  authProvider: "azure", // ASSUMPTION: creai signs in with Microsoft Entra ID; "google" also works
};
