import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Unmount between tests. Without this, queries that assert on absence pass for
// the wrong reason once a previous test has left a tree in the document.
afterEach(cleanup)
