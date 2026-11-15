import Foundation
import SQLite3

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

struct SQLiteError: Error, CustomStringConvertible {
    let description: String
}

/// One row of a result, read by column number.
struct Row {
    fileprivate let st: OpaquePointer

    func isNull(_ i: Int32) -> Bool { sqlite3_column_type(st, i) == SQLITE_NULL }
    func string(_ i: Int32) -> String? { sqlite3_column_text(st, i).map { String(cString: $0) } }
    func text(_ i: Int32) -> String { string(i) ?? "" }
    func int(_ i: Int32) -> Int64 { sqlite3_column_int64(st, i) }
    func optInt(_ i: Int32) -> Int64? { isNull(i) ? nil : int(i) }
    func double(_ i: Int32) -> Double { sqlite3_column_double(st, i) }
    func optDouble(_ i: Int32) -> Double? { isNull(i) ? nil : double(i) }
    func bool(_ i: Int32) -> Bool { int(i) != 0 }
    func data(_ i: Int32) -> Data? {
        guard let p = sqlite3_column_blob(st, i) else { return nil }
        return Data(bytes: p, count: Int(sqlite3_column_bytes(st, i)))
    }
}

/// A SQLite connection: the system library, nothing more. Not thread-safe; `Store` gives each
/// connection a queue of its own.
final class SQLite {
    private var db: OpaquePointer?
    private var prepared: [String: OpaquePointer] = [:]

    init(path: String) throws {
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX, nil) == SQLITE_OK else {
            throw SQLiteError(description: "cannot open \(path): \(message)")
        }
        sqlite3_busy_timeout(db, 10_000)
        try exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
    }

    deinit {
        prepared.values.forEach { sqlite3_finalize($0) }
        sqlite3_close(db)
    }

    private var message: String { db.map { String(cString: sqlite3_errmsg($0)) } ?? "no database" }

    func exec(_ sql: String) throws {
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw SQLiteError(description: "\(message) in \(sql.prefix(80))") }
    }

    private func statement(_ sql: String, _ args: [Any?]) throws -> OpaquePointer {
        var st = prepared[sql]
        if st == nil {
            guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK, st != nil else {
                throw SQLiteError(description: "\(message) in \(sql.prefix(120))")
            }
            if prepared.count < 64 { prepared[sql] = st }
        }
        sqlite3_reset(st)
        sqlite3_clear_bindings(st)
        for (n, a) in args.enumerated() {
            let i = Int32(n + 1)
            switch a {
            case nil, is NSNull: sqlite3_bind_null(st, i)
            case let v as String: sqlite3_bind_text(st, i, v, -1, transient)
            case let v as Bool: sqlite3_bind_int64(st, i, v ? 1 : 0)
            case let v as Int: sqlite3_bind_int64(st, i, Int64(v))
            case let v as Int64: sqlite3_bind_int64(st, i, v)
            case let v as Double: sqlite3_bind_double(st, i, v)
            case let v as Data: _ = v.withUnsafeBytes { sqlite3_bind_blob(st, i, $0.baseAddress, Int32(v.count), transient) }
            case let v as NSNumber:   // JSON numbers: whole ones as integers
                if CFNumberIsFloatType(v) { sqlite3_bind_double(st, i, v.doubleValue) } else { sqlite3_bind_int64(st, i, v.int64Value) }
            default: sqlite3_bind_text(st, i, "\(a!)", -1, transient)
            }
        }
        return st!
    }

    private func done(_ st: OpaquePointer, _ sql: String) {
        sqlite3_reset(st)
        if prepared[sql] == nil { sqlite3_finalize(st) }
    }

    func run(_ sql: String, _ args: [Any?] = []) throws {
        let st = try statement(sql, args)
        defer { done(st, sql) }
        var rc = sqlite3_step(st)
        while rc == SQLITE_ROW { rc = sqlite3_step(st) }
        guard rc == SQLITE_DONE else { throw SQLiteError(description: "\(message) in \(sql.prefix(120))") }
    }

    func query<T>(_ sql: String, _ args: [Any?] = [], _ make: (Row) -> T) throws -> [T] {
        let st = try statement(sql, args)
        defer { done(st, sql) }
        var out: [T] = []
        var rc = sqlite3_step(st)
        while rc == SQLITE_ROW {
            out.append(make(Row(st: st)))
            rc = sqlite3_step(st)
        }
        guard rc == SQLITE_DONE else { throw SQLiteError(description: "\(message) in \(sql.prefix(120))") }
        return out
    }

    func one<T>(_ sql: String, _ args: [Any?] = [], _ make: (Row) -> T) throws -> T? {
        try query(sql, args, make).first
    }

    var lastRowID: Int64 { sqlite3_last_insert_rowid(db) }

    func transaction(_ body: () throws -> Void) throws {
        try exec("BEGIN IMMEDIATE")
        do { try body(); try exec("COMMIT") } catch { try? exec("ROLLBACK"); throw error }
    }
}

/// A database file with one connection for writing and one for reading (WAL lets them overlap), each
/// on its own queue, so nothing here ever runs on the main thread.
final class Store {
    private let writer: SQLite
    private let reader: SQLite
    private let writeQueue = DispatchQueue(label: "store.write", qos: .utility)
    private let readQueue = DispatchQueue(label: "store.read", qos: .userInitiated)

    init(path: String, schema: String) throws {
        writer = try SQLite(path: path)
        try writer.exec(schema)
        reader = try SQLite(path: path)
    }

    func write<T>(_ body: @escaping (SQLite) throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { c in
            writeQueue.async { c.resume(with: Result { try body(self.writer) }) }
        }
    }

    func read<T>(_ body: @escaping (SQLite) throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { c in
            readQueue.async { c.resume(with: Result { try body(self.reader) }) }
        }
    }
}
