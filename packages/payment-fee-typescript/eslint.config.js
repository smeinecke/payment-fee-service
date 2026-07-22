import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";

export default [
  sonarjs.configs.recommended,
  {
    ignores: ["dist/**", "node_modules/**", ".eslintcache"],
  },
  {
    files: ["**/*.ts"],
    plugins: {
      "@typescript-eslint": tseslint.plugin,
    },
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        project: "./tsconfig.lint.json",
      },
    },
    rules: {
      complexity: ["error", 20],
      "sonarjs/cognitive-complexity": ["error", 25],
      "max-depth": ["warn", 4],
      "max-lines-per-function": ["warn", 140],
      "max-params": ["warn", 6],
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      "@typescript-eslint/consistent-type-imports": ["warn", { prefer: "type-imports" }],
      "@typescript-eslint/no-non-null-assertion": "off",
      "no-undef": "off",
    },
  },
  {
    files: ["tests/**/*.test.ts"],
    rules: {
      "max-lines-per-function": "off",
      "max-params": "off",
    },
  },
  {
    files: ["eslint.config.js"],
    rules: {
      "sonarjs/no-hardcoded-passwords": "off",
      "sonarjs/todo-tag": "off",
    },
  },
  // Existing provider files contain inherently complex fee-matching and compilation logic.
  // Keep strict defaults for new code; relax only for these legacy modules.
  {
    files: ["src/providers/**/*.ts"],
    rules: {
      complexity: ["error", 50],
      "sonarjs/cognitive-complexity": ["error", 50],
      "sonarjs/no-alphabetical-sort": "off",
      "sonarjs/no-misleading-array-reverse": "off",
      "sonarjs/no-redundant-optional": "off",
      "sonarjs/unused-import": "off",
      "max-lines-per-function": "off",
    },
  },
  {
    files: ["src/calculator.ts"],
    rules: {
      complexity: ["error", 50],
      "sonarjs/no-invariant-returns": "off",
    },
  },
  {
    files: ["src/errors.ts"],
    rules: {
      "sonarjs/no-alphabetical-sort": "off",
    },
  },
];
