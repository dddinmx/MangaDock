/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        primary: '#E11D48',
        secondary: '#2DD4A8',
        accent: '#F43F5E',
        dark: '#0B0B0F',
        light: '#F3F4F6',
        danger: '#EF4444',
        ink: {
          950: '#07070A',
          900: '#0B0B0F',
          850: '#111118',
          800: '#16161F',
          700: '#1E1E2A',
          600: '#2A2A3A',
          400: '#8B8B9E',
          300: '#A8A8B8',
          200: '#D1D1DB',
          100: '#EDEDF2',
        },
        brand: {
          DEFAULT: '#E11D48',
          soft: '#FB7185',
          mint: '#2DD4A8',
        },
      },
      fontFamily: {
        display: ['"Segoe UI"', 'system-ui', 'sans-serif'],
      },
      maxWidth: {
        site: '72rem',
      },
      boxShadow: {
        hero: '0 24px 80px rgba(0,0,0,0.55)',
        card: '0 12px 40px rgba(0,0,0,0.35)',
      },
    },
  },
  plugins: [],
};
