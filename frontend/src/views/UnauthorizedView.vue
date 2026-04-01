<template>
  <div class="unauthorized-page">
    <div class="error-card">
      <div class="icon">🔒</div>
      <h1>Access Denied</h1>
      <p>
        RetiBoard is running in secure mode. This browser session is not
        authorized to access the local API.
      </p>
      <div class="instruction">
        Please use the <strong>secure URL</strong> provided in your
        terminal to launch the application. It looks like this:
        <code>http://127.0.0.1:8787?token=...</code>
      </div>
      <div class="footer">
        Close this tab and try again with the correct link.
      </div>
    </div>
  </div>
</template>

<style scoped>
.unauthorized-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 1rem;
  background: #0e0e1e;
  font-family: 'Courier New', 'Lucida Console', monospace;
}
.error-card {
  max-width: 500px;
  background: #1a1a2e;
  border: 1px solid #303050;
  border-radius: 8px;
  padding: 2.5rem;
  text-align: center;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}
.icon {
  font-size: 3rem;
  margin-bottom: 1.5rem;
}
h1 {
  color: #ff4d4d;
  margin-bottom: 1rem;
  font-size: 1.5rem;
  text-transform: uppercase;
  letter-spacing: 2px;
}
p {
  color: #c0c0d0;
  line-height: 1.6;
  margin-bottom: 1.5rem;
}
.instruction {
  background: #0a0a15;
  border-radius: 4px;
  padding: 1rem;
  color: #a0a0b0;
  font-size: 0.9rem;
  text-align: left;
  margin-bottom: 1.5rem;
}
code {
  display: block;
  margin-top: 0.5rem;
  color: #7070ff;
  word-break: break-all;
}
.footer {
  color: #606080;
  font-size: 0.8rem;
}
</style>
