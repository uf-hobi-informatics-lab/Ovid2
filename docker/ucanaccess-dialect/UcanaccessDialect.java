package net.sf.ucanaccess.spark;

import org.apache.spark.sql.jdbc.JdbcDialect;

/**
 * Spark {@link JdbcDialect} for the UCanAccess (Microsoft Access) JDBC driver.
 *
 * <p>Why this exists: UCanAccess overlays an Access {@code .accdb} /
 * {@code .mdb} file on HSQLDB with double-quoted identifiers parsed as
 * <em>string literals</em> rather than identifiers. Spark's default JDBC
 * dialect quotes every projected column with double quotes
 * ({@code SELECT "col" FROM t}), so UCanAccess returns the literal
 * column-name string for <em>every</em> row instead of the column value
 * (a table read comes back filled with its own header names). When the
 * column's resolved Catalyst type is numeric, Spark's {@code getInt()}
 * getter instead throws
 * {@code incompatible data type in conversion: from SQL type CHARACTER to
 * java.lang.Integer, value: <column name>}.</p>
 *
 * <p>Access/HSQLDB accept Access-style square-bracket identifiers, so this
 * dialect overrides {@link #quoteIdentifier(String)} to bracket column
 * names. Registered for any {@code jdbc:ucanaccess:*} URL, it lets Spark's
 * native (fast, distributed, lazy) JDBC reader return correct values.</p>
 *
 * <p>Compiled at Docker build time (see {@code docker/Dockerfile}) into a
 * tiny jar baked next to the UCanAccess driver jars; registered on the
 * driver JVM by {@code ovid2.drivers.access.spark._register_dialect}.</p>
 */
public class UcanaccessDialect extends JdbcDialect {

    /** Serialisation id (JdbcDialect implements scala.Serializable). */
    private static final long serialVersionUID = 1L;

    /**
     * Claim every UCanAccess JDBC URL.
     *
     * @param url the JDBC URL Spark is resolving a dialect for
     * @return {@code true} for {@code jdbc:ucanaccess:*} URLs
     */
    @Override
    public boolean canHandle(String url) {
        return url != null
            && url.toLowerCase().startsWith("jdbc:ucanaccess");
    }

    /**
     * Quote a column identifier the way UCanAccess/HSQLDB accepts it:
     * Access-style square brackets. Any embedded {@code ']'} is stripped
     * defensively (Access forbids it in object names), which also closes
     * the only bracket-injection vector.
     *
     * @param colName the raw column name
     * @return {@code colName} wrapped in square brackets
     */
    @Override
    public String quoteIdentifier(String colName) {
        return "[" + colName.replace("]", "") + "]";
    }
}
