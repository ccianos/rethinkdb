"""This module implements a way to access RethinkDB clusters over the
network. It is used to send ReQL commands to RethinkDB in order to do
data manipulation."""

__all__ = ["QueryError", "ExecutionError", "BadQueryError",
    "BatchedIterator",
    "Connection", "connect"]

import json
import socket
import struct

import query_language_pb2 as p
import query
import internal

last_connection = None

PRETTY_PRINT_BEGIN_TARGET = "\0begin\0"
PRETTY_PRINT_END_TARGET = "\0end\0"

class BacktracePrettyPrinter(internal.PrettyPrinter):
    def __init__(self, current_backtrace, target_backtrace):
        self.current_backtrace = current_backtrace
        self.target_backtrace = target_backtrace

    def consider_backtrace(self, string, backtrace_steps):
        complete_backtrace = self.current_backtrace + backtrace_steps
        if complete_backtrace == self.target_backtrace:
            assert PRETTY_PRINT_BEGIN_TARGET not in string
            assert PRETTY_PRINT_END_TARGET not in string
            return PRETTY_PRINT_BEGIN_TARGET + string + PRETTY_PRINT_END_TARGET
        else:
            prefix_match_length = 0
            while True:
                if prefix_match_length == len(complete_backtrace):
                    # We're on the path to the target term.
                    return string
                elif prefix_match_length == len(self.target_backtrace):
                    # We're a sub-term of the target term.
                    if len(complete_backtrace) > len(self.target_backtrace) + 2 or len(string) > 60:
                        # Don't keep recursing for very long after finding the target
                        return "..." if len(string) > 8 else string
                    else:
                        return string
                else:
                    if complete_backtrace[prefix_match_length] == self.target_backtrace[prefix_match_length]:
                        prefix_match_length += 1
                    else:
                        # We're not on the path to the target term.
                        if len(complete_backtrace) > prefix_match_length + 2 or len(string) > 60:
                            # Don't keep recursing for very long on a side branch of the tree.
                            return "..." if len(string) > 8 else string
                        else:
                            return string

    def expr_wrapped(self, expr, backtrace_steps):
        string, wrapped = expr._inner.pretty_print(BacktracePrettyPrinter(self.current_backtrace + backtrace_steps, self.target_backtrace))
        if wrapped == internal.PRETTY_PRINT_EXPR_UNWRAPPED:
            string = "expr(%s)" % string
        return self.consider_backtrace(string, backtrace_steps)

    def expr_unwrapped(self, expr, backtrace_steps):
        string = expr._inner.pretty_print(BacktracePrettyPrinter(self.current_backtrace + backtrace_steps, self.target_backtrace))[0]
        return self.consider_backtrace(string, backtrace_steps)

    def write_query(self, wq, backtrace_steps):
        string = wq._inner.pretty_print(BacktracePrettyPrinter(self.current_backtrace + backtrace_steps, self.target_backtrace))
        return self.consider_backtrace(string, backtrace_steps)

    def simple_string(self, string, backtrace_steps):
        return self.consider_backtrace(string, backtrace_steps)

class QueryError(StandardError):
    def __init__(self, message, ast_path, query):
        self.message = message
        self.ast_path = ast_path
        self.query = query

    def location(self):
        printer = BacktracePrettyPrinter([], self.ast_path)
        if isinstance(self.query, query.ReadQuery):
            query_str = printer.expr_wrapped(self.query, [])
        elif isinstance(self.query, query.WriteQuery):
            query_str = printer.write_query(self.query, [])
        elif isinstance(self.query, query.MetaQuery):
            raise NotImplementedError()

        # Draw a row of carets under the part of `query_str` that is bracketed
        # by `PRETTY_PRINT_BEGIN_TARGET` and `PRETTY_PRINT_END_TARGET`.
        if not (query_str.count(PRETTY_PRINT_BEGIN_TARGET) == query_str.count(PRETTY_PRINT_END_TARGET) == 1):
            raise ValueError("Internal error: can't follow path %r in %r" % (self.ast_path, self.query))
        formatted_lines = []
        in_target = False
        for line in query_str.split("\n"):
            line = line.rstrip(" ")
            if not in_target:
                if PRETTY_PRINT_BEGIN_TARGET in line:
                    if PRETTY_PRINT_END_TARGET in line:
                        before, rest = line.split(PRETTY_PRINT_BEGIN_TARGET)
                        target, after = rest.split(PRETTY_PRINT_END_TARGET)
                        formatted_lines.append(before + target + after)
                        formatted_lines.append(" " * len(before) + "^" * len(target))
                    else:
                        before, after = line.split(PRETTY_PRINT_BEGIN_TARGET)
                        formatted_lines.append(before + after)
                        formatted_lines.append(" " * len(before) + "^" * len(after))
                        in_target = True
                else:
                    formatted_lines.append(line)
            else:
                without_spaces = line.lstrip(" ")
                spaces = " " * (len(line) - len(without_spaces))
                if PRETTY_PRINT_END_TARGET in without_spaces:
                    before, after = without_spaces.split(PRETTY_PRINT_END_TARGET)
                    formatted_lines.append(spaces + before + after)
                    formatted_lines.append(spaces + "^" * len(before))
                    in_target = False
                else:
                    formatted_lines.append(spaces + without_spaces)
                    formatted_lines.append(spaces + "^" * len(without_spaces))
        return "\n".join(formatted_lines)

class ExecutionError(QueryError):
    def __str__(self):
        return "Error while executing query on server: %s" % self.message + "\n" + self.location()

class BadQueryError(QueryError):
    def __str__(self):
        return "Illegal query: %s" % self.message + "\n" + self.location()

class BatchedIterator(object):
    """A result stream from the server that lazily fetches results"""
    def __init__(self, conn, query, token, data, complete):
        self.conn = conn
        self.query = query
        self.token = token
        self.data = data
        self.complete = complete

        self.more_query = p.Query()
        self.more_query.token = token
        self.more_query.type = p.Query.CONTINUE

    def read_more(self):
        if self.complete:
            return

        more_data, status = self.conn._run(self.more_query, self.query)

        if status == p.Response.SUCCESS_STREAM:
            self.complete = True

        self.data += more_data

    def read_until(self, index):
        if index is None:
            while not self.complete:
                self.read_more()
        elif index >= len(self.data):
            while not self.complete and index >= len(self.data):
                self.read_more()

    def __iter__(self):
        index = 0
        while not self.complete and index < len(self.data):
            self.read_until(index)
            yield self.data[index]
            index += 1

    def __getitem__(self, index):
        if isinstance(index, slice):
           self.read_until(index.end)
           return self.data[index]
        if index < 0:
            self.read_until(None)
        else:
            self.read_until(index)
        return self.data[index]

    def __eq__(self, other):
        if isinstance(other, list):
            self.read_until(len(other))
            return self.complete and self.data == other
        return NotImplemented

    def __repr__(self):
        return 'BachedIterator(query=%s, token=%s): data=[%s]' % (
            self.query, self.token, ', '.join(map(str, self.data))
            + ('...', '')[self.complete])

class Connection():
    """A network connection to the RethinkDB cluster. Queries may be
    evaluated via this connection object. The connection automatically
    tries to route queries to the appropriate server node to minimize
    network hops.

    The connection may be used with Python's `with` statement for
    exception safety.
    """
    def __init__(self, host_or_list=None, port=None, db_name=None):
        """Connect to a RethinkDB cluster. The connection may be
        created by specifying the host (in which case the default port
        will be used), host and port, no arguments (in which case the
        default port on localhost will be used), or a list of values
        where each value contains the host string or a tuple of (host,
        port) pairs.

        Once the connection object reaches a single node, it retreives
        the addresses of other nodes and connects to them
        automatically. Queries are then routed to the most appropriate
        node to minimize network hops on the server.

        If all of the nodes are inaccessible, or if all of the nodes
        terminate their network connections, the connection object
        raises an error. If some of the nodes are inaccessible, or if
        some of the nodes terminate their network connections, the
        connection object does not attempt to connect to them again.

        Use :func:`rethinkdb.connect` - as shorthand for
        this method.

        Creating a connection sets :data:`last_connection` to
        itself. This is used by :func:`rethinkdb.query.Expression.run`
        as a default connection if no connection object is passed.

        :param host_or_list: A hostname, or a list of hostnames or
          (host, port) tuples.
        :type host_or_list: str or a list of values where each value
          may be an str, or an (str, int) tuple.
        :param port: The port to connect to. If not specified, the
          default port is used. If `host_or_port` argument is a list,
          `port` is ignored.
        :type port: int

        :param db_name: An optional name of a database to be used by
          default for expression that don't specify a database
          explicitly. Equivalent to calling :func:`use`.
        :type db_name: str
        """
        self.token = 1
        self.socket = socket.create_connection((host_or_list, port))
        self.socket.sendall(struct.pack("<L", 0xaf61ba35))

    def _get_token(self):
        token = self.token
        self.token += 1
        return token

    def _recvall(self, length):
        buf = ""
        while len(buf) != length:
            buf += self.socket.recv(length - len(buf))
        return buf

    def _run(self, protobuf, query, debug=False):
        if debug:
            print "sending:", protobuf

        serialized = protobuf.SerializeToString()

        header = struct.pack("<L", len(serialized))
        self.socket.sendall(header + serialized)
        resp_header = self._recvall(4)
        msglen = struct.unpack("<L", resp_header)[0]
        response_serialized = self._recvall(msglen)
        response = p.Response()
        response.ParseFromString(response_serialized)

        if debug:
            print "response:", response

        code = response.status_code

        if code == p.Response.SUCCESS_JSON:
            return json.loads(response.response[0]), code
        elif code in (p.Response.SUCCESS_STREAM, p.Response.SUCCESS_PARTIAL):
            return [json.loads(s) for s in response.response], code
        elif code == p.Response.SUCCESS_EMPTY:
            return None, code
        elif code == p.Response.RUNTIME_ERROR:
            raise ExecutionError(response.error_message, response.backtrace.frame, query)
        elif code == p.Response.BAD_QUERY:
            raise BadQueryError(response.error_message, response.backtrace.frame, query)
        elif code == p.Response.BROKEN_CLIENT:
            raise ValueError("RethinkDB server rejected our protocol buffer as "
                "malformed. RethinkDB client is buggy?")
        else:
            raise ValueError("Got unexpected status code from server: %d" % response.status_code)


    def run(self, expr, debug=False):
        """Evaluate the expression or list of expressions `expr` on
        the server using this connection. If `expr` is a list,
        evaluates them on the server in order - this can be used to
        pack multiple expressions together to save network roundtrips.

        Use :func:`rethinkdb.query.Expression.run` - as shorthand for
        this method.

        :param expr: An expression or a list of expressions to be
          evaluated on the server.
        :type expr: :class:`rethinkdb.query.Expression`, list of
          :class:`rethinkdb.query.Expression`

        :returns: The return value depends on the expression being
          evaluated. It may be a JSON value, a
          :class:`rethinkdb.net.BatchedIterator`, or nothing. See the
          documentation for the specific expression being evaluated
          for more details. If `expr` is a list, returns a list of
          results, where each element is a result for each individual
          expression.

        :raises: :class:`rethinkdb.net.QueryError` in case of a server
          error.

        :Example:

        >>> q = table('table_name').all()
        >>> conn.run(q) # returns an iterator
        >>> q1 = table('another_table').all()
        >>> q2 = table('yet_another_table').all()
        >>> conn.run([q1, q2]) # returns a list of two iterators
        """
        protobuf = p.Query()
        protobuf.token = self._get_token()
        expr._finalize_query(protobuf)
        ret, code = self._run(protobuf, expr, debug)

        if code in (p.Response.SUCCESS_STREAM, p.Response.SUCCESS_PARTIAL):
            return BatchedIterator(self, expr, protobuf.token, ret, code == p.Response.SUCCESS_STREAM)

        return ret

    def use(self, db_name):
        """Sets the default database for this connection. All queries
        that don't explicitly specify a database will use the database
        set by this method.

        :param db_name: The name of the database to use by default.
        :type db_name: str

        :Example:

        >>> q = table('users').all()
        >>> conn.use('foo')
        >>> conn.run(q)      # select all users from database 'foo'
        >>> conn.use('bar')
        >>> conn.run(q)      # select all users from database 'bar'
        """
        pass

    def close(self):
        """Closes all network sockets on this connection object to the
        cluster."""
        pass

def connect(host_or_list=None, port=None, db_name=None):
    """
    Creates a :class:`Connection` object. This method is a shorthand
    for constructing the :class:`Connection` object directly.

    :returns: :class:`Connection` -- a connection object that can be
      used to communicate with the RethinkDB cluster.

    :Example:

    >>> conn = connect() # localhost, default port
    >>> conn = connect('electro') # host 'electro', default port
    >>> conn = connect('electro', 8080) # host 'electro', port 8080
    >>> conn = connect([('electro', 8080),
    >>>                 'magneto',
    >>>                 ('puzzler', 8181)])
    """
    return Connection(host_or_list, port, db_name)

