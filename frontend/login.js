const HOST = window.location.hostname || 'localhost';
const PORT = window.location.port;
const PORT_SUFFIX = PORT ? `:${PORT}` : '';
const API_URL = `${window.location.protocol}//${HOST}${PORT_SUFFIX}`;

let mode = 'login';

document.querySelectorAll('.auth-tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.auth-tab-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    mode = btn.dataset.mode;
    document.getElementById('authSubmit').textContent = mode === 'login' ? 'Sign In' : 'Create Account';
    document.getElementById('authError').textContent = '';
  });
});

document.getElementById('authForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errBox = document.getElementById('authError');
  errBox.textContent = '';
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;

  try {
    const res = await fetch(`${API_URL}/api/auth/${mode === 'login' ? 'login' : 'register'}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      errBox.textContent = data.error || 'Something went wrong.';
      return;
    }
    if (mode === 'login') {
      window.location.href = '/';
    } else {
      // registered -- now log in automatically
      const loginRes = await fetch(`${API_URL}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ username, password }),
      });
      if (loginRes.ok) window.location.href = '/';
      else errBox.textContent = 'Account created -- please sign in.';
    }
  } catch {
    errBox.textContent = 'Could not reach the backend.';
  }
});
