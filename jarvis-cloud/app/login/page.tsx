export default function LoginPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950">
      <div className="w-full max-w-sm p-8 border border-zinc-800 rounded-lg">
        <h1 className="text-2xl font-bold text-zinc-100 mb-2 font-mono">JARVIS</h1>
        <p className="text-zinc-500 text-sm mb-8">Trinity Nervous System</p>
        <button className="w-full bg-zinc-100 text-zinc-900 font-medium py-3 rounded-md hover:bg-zinc-200 transition-colors" id="webauthn-login">
          Sign in with Passkey
        </button>
        <p className="text-zinc-600 text-xs mt-4 text-center">Touch ID · Face ID · Security Key</p>
      </div>
    </div>
  );
}
