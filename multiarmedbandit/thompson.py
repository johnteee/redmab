from __future__ import division
import redpipe


# still in progress, do not use.
draw_lua="""
local name = KEYS[1]
local arms = ARGV
local max_mean = 0
local mean = 0
local count = 0
local arm = arms[1]
local bulk = redis.call('HGETALL', name)
local result = {}
local k, i, v

for i, v in ipairs(bulk) do
    if i % 2 == 1 then
        k = v
    else
        result[k] = v
    end
end

for i, a in ipairs(arms) do
    mean = tonumber(result["#{" .. a .. "}:mean"] or 0)
    if mean > max_mean then
        max_mean = mean
        arm = a
    end
end

redis.call('HINCRBY', name, "#{" .. arm .. "}:count", 1)
mean = tonumber(result["#{" .. arm .. "}:mean"])
local success = tonumber(result["#{" .. arm .. "}:success"] or 0)
local count = tonumber(result["#{" .. arm .. "}:count"] or 0) + 1
local alpha = tonumber(result["alpha"] or 0)
local beta = tonumber(result["beta"] or 0)

mean = 1 / (1 + (count - success) + beta / (success + alpha))

redis.call('HSET', name, "#{" .. arm .. "}:mean", mean)
return arm
"""

class ThompsonMultiArmedBandit(object):
    USE_LUA = False

    @classmethod
    def beta_mean(cls, success, count, alpha, beta):
        fail_count = count - success
        return 1 / (1 + float(fail_count + beta) / (success + alpha))

    @classmethod
    def klass(cls, conn, keysp):
        class storage(redpipe.Hash):
            keyspace = conn
            connection = keysp

        return storage

    def __init__(self, name, arms, connection=None, keyspace='TMAB', alpha=5, beta=5):

        self.name = name
        self.alpha = alpha
        self.beta = beta
        self.arms = set(arms)
        self.init_mean = self.beta_mean(success=0, count=0, alpha=self.alpha, beta=self.beta)
        self.storage = self.klass(connection, keyspace)
        #self._create(pipe=pipe)

    def _pipe(self, pipe=None, autoexec=False):
        return redpipe.pipeline(pipe=pipe, autoexec=autoexec)

    def _means_k(self, arm):
        return '#{%s}:mean' % arm

    def _success_k(self, arm):
        return '#{%s}:success' % arm

    def _count_k(self, arm):
        return '#{%s}:count' % arm

    def delete(self, pipe=None):
        with self._pipe(pipe=pipe, autoexec=True) as p:
            s = self.storage(pipe=p)
            s.delete(self.name)

    def draw(self, pipe=None):
        if self.USE_LUA:
            return self.storage(pipe=pipe).eval(self.name, draw_lua, *[a for a in self.arms])

        arm_means = {k: 0 for k in self.arms}
        with self._pipe(autoexec=True) as p:
            s = self.storage(pipe=p)
            arm_means = {k: s.hget(self.name, self._means_k(k)) for k in self.arms}

        max_arm = max(arm_means.keys(), key=lambda k: float(arm_means[k]) if arm_means[k] else self.init_mean)

        with self._pipe(autoexec=True) as p:
            s = self.storage(pipe=p)
            s.hincrby(self.name, self._count_k(max_arm), 1)


            def cb():
                mean = self.mean(max_arm)
                self.update_mean(arm=max_arm, mean=mean)

            p.on_execute(cb)

        return max_arm

    def draw_multi(self, times):
        return [self.draw() for _ in range(times)]

    def update_sucess(self, arm, reward=1.0):
        with self._pipe(autoexec=True) as p:
            s = self.storage(pipe=p)
            s.hincrbyfloat(self.name, self._success_k(arm), reward)
            mean = self.mean(arm=arm, pipe=p)

            def cb():
                self.update_mean(arm=arm, mean=mean)

            p.on_execute(cb)

    def update_mean(self, arm, mean, pipe=None):
        with self._pipe(pipe=pipe, autoexec=True) as p:
            s = self.storage(pipe=p)
            s.hset(self.name, self._means_k(arm), str(float(mean)))

    def enable(self, arm):
        self.update_mean(arm, self.mean(arm))

    def mean(self, arm, pipe=None):
        with self._pipe(pipe=pipe, autoexec=True) as p:
            s = self.storage(pipe=p)
            success = s.hget(self.name, self._success_k(arm))
            count = s.hget(self.name, self._count_k(arm))

            future = redpipe.Future()

            def cb():
                s = float(success or 0)
                c = float(count or 0)

                future.set(self.beta_mean(success=s, count=c, alpha=self.alpha, beta=self.beta))

            p.on_execute(cb)

        return future

    def stats(self, pipe=None):
        with self._pipe(pipe=pipe, autoexec=True) as p:
            s = self.storage(pipe=p)
            state = s.hgetall(self.name)

        return state
