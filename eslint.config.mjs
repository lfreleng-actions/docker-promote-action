// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 The Linux Foundation

// ESLint flat configuration.
//
// Inert until the repository contains JavaScript or TypeScript. The
// pre-commit hook runs repository-wide and guards on package.json,
// testing the git index rather than the filesystem so an untracked
// manifest cannot satisfy it. With no tracked manifest it exits 0
// without invoking ESLint, unless lintable sources are still tracked,
// in which case it fails rather than letting a deleted or unstaged
// manifest silently disable linting. A manifest tracked in a
// subdirectory counts, but only for the sources beneath it: a package
// workspace is a legitimate layout, so the hook stays inert instead of
// demanding a root package.json that was never deleted, while sources
// outside any workspace still fail rather than being masked by it. Where a root package.json is tracked it runs the
// project's own ESLint over the repository, pinned to this file so
// discovery cannot walk up into a config outside the checkout.
//
// ESLint 9 made flat config the default while still honouring
// ESLINT_USE_FLAT_CONFIG=false to load a legacy .eslintrc; ESLint 10
// removes the legacy format entirely. The hook forces flat-config mode
// so an inherited environment variable cannot bypass this file.
//
// The hook no longer relies on ESLint's own discovery error. It checks
// that this file is tracked and passes it explicitly with
// --no-config-lookup, because flat-config discovery walks up the
// directory tree and would otherwise lint against a config outside the
// checkout, and because --config naming an absent file raises a
// filesystem stack trace rather than an actionable message. The file
// ships with the template so a new project lints from its first commit.
//
// Adopting repositories need eslint, @eslint/js, typescript-eslint,
// typescript and globals in devDependencies; the hook runs the project's
// own ESLint. typescript is a required peer of typescript-eslint, which
// this file imports unconditionally, so it is needed even by
// JavaScript-only projects under package managers that do not install
// peer dependencies automatically.

import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    // Build output and vendored code are not ours to lint. Flat config
    // has no cascading .eslintignore, so ignores belong here.
    //
    // Patterns are recursive ('**/dist/**' rather than 'dist/**') so that
    // generated output inside nested packages is excluded too, not just
    // directories sitting beside this file.
    ignores: [
      '**/dist/**',
      '**/build/**',
      '**/coverage/**',
      '**/node_modules/**',
    ],
  },

  js.configs.recommended,

  // Type-aware rules are deliberately not enabled: they require a
  // tsconfig.json and a project service, which a plain JavaScript
  // repository does not have. Projects with TypeScript can opt in by
  // switching to tseslint.configs.recommendedTypeChecked.
  ...tseslint.configs.recommended,

  {
    // ESLint only considers '**/*.js', '**/*.mjs' and '**/*.cjs' by
    // default, and the TypeScript preset adds its own matchers. Neither
    // covers '.jsx', so a staged .jsx file the hook passes in would be
    // rejected with "no matching configuration was supplied" rather than
    // linted. Declaring the matcher here keeps it aligned with the hook's
    // own file pattern.
    files: ['**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      // The pre-commit hook accepts .jsx, but the TypeScript ESLint
      // parser only enables JSX automatically for .tsx. Without this, a
      // .jsx file containing JSX fails with a parse error rather than a
      // lint finding.
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
      // js.configs.recommended enables no-undef. Without Node's globals
      // declared, ordinary action code referencing process, Buffer,
      // console or the timer functions fails to lint.
      //
      // nodeBuiltin rather than node: the latter also declares the
      // CommonJS-only names (module, exports, __dirname, __filename),
      // which do not exist in ESM. Declaring them here would stop
      // no-undef reporting them when they are used by mistake in a
      // module, which is exactly the error worth catching. The CommonJS
      // overrides below add the full set where those names are real.
      globals: {
        ...globals.nodeBuiltin,
      },
    },
  },

  {
    // CommonJS entry points still appear in GitHub Actions bundles, where
    // 'require' and 'module' are legitimate globals.
    files: ['**/*.cjs', '**/*.cts'],
    languageOptions: {
      sourceType: 'commonjs',
      // The full Node set here, adding module, exports, __dirname and
      // __filename on top of the builtins declared above.
      globals: {
        ...globals.node,
      },
    },
    rules: {
      // sourceType only governs parsing. tseslint's recommended set also
      // enables no-require-imports, which would still reject the very
      // require() calls that make these files CommonJS.
      '@typescript-eslint/no-require-imports': 'off',
    },
  },

  // ESLint does not infer CommonJS for '.js' from the absence of
  // '"type": "module"' in package.json, so the module default above
  // applies to every '.js' file. That suits a new project, which is what
  // this template seeds.
  //
  // A repository whose '.js' sources use require()/module.exports must
  // uncomment the block below, or ESLint reports 'require is not defined'
  // against perfectly valid code. Several existing action repositories
  // are in exactly that position. Note the globals and rule override as
  // well as the sourceType: without them, __dirname is undeclared and
  // no-require-imports still fails the file.
  //
  // {
  //   files: ['**/*.js'],
  //   languageOptions: {
  //     sourceType: 'commonjs',
  //     globals: {
  //       ...globals.node,
  //     },
  //   },
  //   rules: {
  //     '@typescript-eslint/no-require-imports': 'off',
  //   },
  // },
);
