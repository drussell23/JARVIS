import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import CommandCenter from './components/CommandCenter/CommandCenter';
import reportWebVitals from './reportWebVitals';

// Slice 110 — minimal, dependency-free view switch (no react-router needed).
// The Sovereign Command Center mounts at /command-center (or ?view=command-center
// or #command-center); every other path renders the standard JARVIS App.
function selectRoot() {
  try {
    const { pathname, hash, search } = window.location;
    const wantsCC =
      pathname.replace(/\/+$/, '').endsWith('/command-center') ||
      hash.includes('command-center') ||
      new URLSearchParams(search).get('view') === 'command-center';
    return wantsCC ? <CommandCenter /> : <App />;
  } catch (_e) {
    return <App />;
  }
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    {selectRoot()}
  </React.StrictMode>
);

// If you want to start measuring performance in your app, pass a function
// to log results (for example: reportWebVitals(console.log))
// or send to an analytics endpoint. Learn more: https://bit.ly/CRA-vitals
reportWebVitals();
