/**
 * PostCSS config -- wires Tailwind + autoprefixer for the Command Node.
 * Tailwind's theme is extended from the :root token block (see
 * tailwind.config.js + app/globals.css).
 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
