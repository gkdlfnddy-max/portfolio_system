import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./data/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: "#4F7CFF",
          50: "#EEF3FF",
          100: "#D9E3FF",
          500: "#4F7CFF",
          600: "#3D6AEB",
          700: "#2F58D6",
        },
        accent: {
          DEFAULT: "#FF8A4C",
          500: "#FF8A4C",
          600: "#F37232",
        },
        neutral: {
          50: "#F7F8FA",
          100: "#EEF0F4",
          200: "#DCE0E8",
          400: "#9099A8",
          700: "#3B4150",
          900: "#1F2430",
        },
        success: "#16A34A",
        warning: "#D97706",
        error: "#DC2626",
        info: "#2563EB",
      },
      borderRadius: {
        xl: "16px",
        "2xl": "20px",
      },
      boxShadow: {
        card: "0 4px 16px rgba(0, 0, 0, 0.06)",
        "card-hover": "0 8px 24px rgba(0, 0, 0, 0.10)",
      },
      fontFamily: {
        sans: ["Pretendard", "system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;
